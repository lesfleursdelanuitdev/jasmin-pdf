"""Shopify integration: webhook verification, the storefront snippet and an
optional Admin API client.

How the pieces fit:

1. The store has the Jasmin app installed (``shopify.app.toml``; made in the
   store's Dev Dashboard). Its theme app extension (``extensions/jasmin-personalize``)
   gives the theme editor a "Personalize button" block for the product page. The
   block loads ``jasmin-personalize.js``, which shows a "Personalize" button for
   products linked to a template and opens the editor in a modal. On a theme
   without app blocks, the *Custom Liquid* snippet (:func:`liquid_snippet`) does
   the same.
2. When the customer approves their proof, the editor posts the design id to
   the storefront page, which adds the variant to the cart with line item
   properties (``_design_id`` is hidden from the customer, ``Personalization``
   is shown in the cart and on the order).
3. Shopify sends ``orders/create`` to ``/webhooks/shopify/orders-create`` (the app
   subscribes to it in its configuration); we verify the HMAC signature, made with
   the app's client secret, store the order and render the print files.
4. With the app's client id and secret we get an Admin API token from Shopify
   (client credentials; it lasts a day and is renewed here) to tag the orders.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import threading
import time

import httpx

from .config import Settings

log = logging.getLogger("jasmin.shopify")


def sign(body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def verify_webhook(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not secret:
        return False
    return hmac.compare_digest(sign(body, secret), header.strip())


def liquid_snippet(app_url: str) -> str:
    return f"""{{%- comment -%}}
  Jasmin personalisation. Paste into a "Custom Liquid" block on the product
  page template (Online Store > Themes > Customize > Products > Default product).
  Products are linked to templates in the Jasmin admin (by product handle).
{{%- endcomment -%}}
<div class="jasmin-personalize"
     data-app-url="{app_url}"
     data-product-handle="{{{{ product.handle }}}}"
     data-variant-id="{{{{ product.selected_or_first_available_variant.id }}}}"
     data-button-label="Personalize"
     data-hide-add-to-cart="true"></div>
<script src="{app_url}/static/shopify/jasmin-personalize.js" defer></script>
"""


BLOCK_HANDLE = "personalize"  # extensions/jasmin-personalize/blocks/personalize.liquid


def add_block_url(shop_domain: str, client_id: str) -> str:
    """The theme editor, opened on the product page with the Personalize button ready to be placed."""
    return (
        f"https://{shop_domain}/admin/themes/current/editor?template=product"
        f"&addAppBlockId={client_id}/{BLOCK_HANDLE}&target=mainSection"
    )


class ShopifyError(RuntimeError):
    pass


class ShopifyAdmin:
    """Tiny GraphQL Admin API client; a no-op unless the store and a way to get a token are configured.

    The token is either a legacy custom app's fixed one (SHOPIFY_ADMIN_TOKEN) or, for an
    app from the Dev Dashboard, one that Shopify hands out for the app's client id and
    secret. That one lasts 24 hours, so it is kept and renewed a minute before it runs out.
    """

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._http = httpx.Client(timeout=10, transport=transport)
        self._token: tuple[str, float] = ("", 0.0)
        self._lock = threading.Lock()
        self.scopes: list[str] = []

    @property
    def _credentials(self) -> bool:
        return bool(self.settings.shopify_client_id and self.settings.shopify_client_secret)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.shopify_shop_domain and (self.settings.shopify_admin_token or self._credentials))

    def _access_token(self) -> str:
        if self.settings.shopify_admin_token:
            return self.settings.shopify_admin_token
        with self._lock:
            token, until = self._token
            if token and time.monotonic() < until:
                return token
            try:
                resp = self._http.post(
                    f"https://{self.settings.shopify_shop_domain}/admin/oauth/access_token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.settings.shopify_client_id,
                        "client_secret": self.settings.shopify_client_secret,
                    },
                )
            except httpx.HTTPError as exc:
                raise ShopifyError(f"Shopify could not be reached: {exc}") from exc
            if resp.status_code != 200:
                raise ShopifyError(
                    f"Shopify refused the app's credentials (HTTP {resp.status_code}). Check the client id and secret, "
                    "that the app is installed on this store, and that the store belongs to the app's organisation."
                )
            try:
                body = resp.json()
                token = str(body["access_token"])
            except (ValueError, KeyError) as exc:
                raise ShopifyError("Shopify's answer had no access token") from exc
            self.scopes = sorted(x for x in str(body.get("scope", "")).split(",") if x)
            self._token = (token, time.monotonic() + max(60.0, float(body.get("expires_in", 86399)) - 60))
            return token

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        url = f"https://{self.settings.shopify_shop_domain}/admin/api/{self.settings.shopify_api_version}/graphql.json"
        for attempt in (1, 2):
            token = self._access_token()
            try:
                resp = self._http.post(url, json={"query": query, "variables": variables or {}}, headers={"X-Shopify-Access-Token": token})
            except httpx.HTTPError as exc:
                raise ShopifyError(f"Shopify could not be reached: {exc}") from exc
            if resp.status_code == 401 and attempt == 1 and not self.settings.shopify_admin_token:
                self._token = ("", 0.0)  # withdrawn before its time: ask for a new one, once
                continue
            if resp.status_code != 200:
                raise ShopifyError(f"Shopify answered HTTP {resp.status_code}")
            try:
                body = resp.json()
            except ValueError as exc:
                raise ShopifyError("Shopify's answer could not be read") from exc
            if body.get("errors"):
                raise ShopifyError(str(body["errors"])[:300])
            return body.get("data") or {}
        raise AssertionError("unreachable")

    def check(self) -> dict:
        """Whether the store can be reached with the configured credentials, said in a way the admin can show."""
        if not self.settings.shopify_shop_domain:
            return {"ok": False, "error": "No store is set (SHOPIFY_SHOP_DOMAIN)."}
        if not self.enabled:
            return {"ok": False, "error": "The app's client id and secret are not set."}
        try:
            shop = self._graphql("{ shop { name myshopifyDomain } }").get("shop") or {}
        except ShopifyError as exc:
            return {"ok": False, "error": str(exc)}
        missing = [x for x in ("read_orders", "write_orders") if self.scopes and x not in self.scopes]
        return {"ok": True, "shop": shop.get("name", ""), "domain": shop.get("myshopifyDomain", ""), "scopes": self.scopes, "missingScopes": missing}

    def add_order_tags(self, order_id: str, tags: list[str]) -> bool:
        if not self.enabled or not order_id.isdigit():
            return False
        query = (
            "mutation addTags($id: ID!, $tags: [String!]!) {"
            " tagsAdd(id: $id, tags: $tags) { userErrors { field message } } }"
        )
        try:
            data = self._graphql(query, {"id": f"gid://shopify/Order/{order_id}", "tags": tags})
        except ShopifyError as exc:
            log.warning("Shopify Admin API call failed: %s", exc)
            return False
        errors = (data.get("tagsAdd") or {}).get("userErrors") or []
        if errors:
            log.warning("tagsAdd failed for order %s: %s", order_id, errors)
            return False
        return True

    def close(self) -> None:
        self._http.close()
