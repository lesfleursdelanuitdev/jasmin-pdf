"""Shopify integration: webhook verification, the storefront snippet and an
optional Admin API client.

How the pieces fit:

1. A *Custom Liquid* block on the product page (see :func:`liquid_snippet`)
   loads ``static/shopify/jasmin-personalize.js``.  It shows a "Personalize"
   button for products linked to a template and opens the editor in a modal.
2. When the customer approves their proof, the editor posts the design id to
   the storefront page, which adds the variant to the cart with line item
   properties (``_design_id`` is hidden from the customer, ``Personalization``
   is shown in the cart and on the order).
3. Shopify sends ``orders/create`` to ``/webhooks/shopify/orders-create``; we
   verify the HMAC signature, store the order and render the print files.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging

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


class ShopifyAdmin:
    """Tiny GraphQL Admin API client; a no-op unless a token is configured."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.shopify_admin_token and self.settings.shopify_shop_domain)

    def add_order_tags(self, order_id: str, tags: list[str]) -> bool:
        if not self.enabled or not order_id.isdigit():
            return False
        url = (
            f"https://{self.settings.shopify_shop_domain}/admin/api/"
            f"{self.settings.shopify_api_version}/graphql.json"
        )
        query = (
            "mutation addTags($id: ID!, $tags: [String!]!) {"
            " tagsAdd(id: $id, tags: $tags) { userErrors { field message } } }"
        )
        try:
            resp = httpx.post(
                url,
                json={"query": query, "variables": {"id": f"gid://shopify/Order/{order_id}", "tags": tags}},
                headers={"X-Shopify-Access-Token": self.settings.shopify_admin_token},
                timeout=10,
            )
            resp.raise_for_status()
            errors = resp.json().get("data", {}).get("tagsAdd", {}).get("userErrors") or []
            if errors:
                log.warning("tagsAdd failed for order %s: %s", order_id, errors)
                return False
            return True
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Shopify Admin API call failed: %s", exc)
            return False
