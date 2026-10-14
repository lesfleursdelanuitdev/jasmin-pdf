"""The store's Jasmin app: an Admin API token from the app's credentials, the connection test, and the theme block."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app
from app.shopify import ShopifyAdmin, add_block_url, sign

SHOP = "jasmin-prints.myshopify.com"
EXT = ROOT / "extensions" / "jasmin-personalize"


class FakeShopify:
    """Shopify's token endpoint and GraphQL Admin API, for one store and one app."""

    def __init__(self):
        self.tokens = 0
        self.calls: list[dict] = []
        self.scope = "read_orders,write_orders"
        self.reject_credentials = False
        self.expire_next = False
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == SHOP
        if request.url.path == "/admin/oauth/access_token":
            form = parse_qs(request.content.decode())
            if self.reject_credentials or form != {"grant_type": ["client_credentials"], "client_id": ["cid"], "client_secret": ["csecret"]}:
                return httpx.Response(400, json={"error": "invalid_client"})
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"token-{self.tokens}", "scope": self.scope, "expires_in": 86399})
        assert re.fullmatch(r"/admin/api/\d{4}-\d{2}/graphql\.json", request.url.path), request.url.path
        if self.expire_next or request.headers.get("X-Shopify-Access-Token") != f"token-{self.tokens}":
            self.expire_next = False
            return httpx.Response(401, json={"errors": "Invalid API key or access token"})
        body = json.loads(request.content)
        self.calls.append(body)
        if "shop {" in body["query"]:
            return httpx.Response(200, json={"data": {"shop": {"name": "Jasmin Prints", "myshopifyDomain": SHOP}}})
        return httpx.Response(200, json={"data": {"tagsAdd": {"userErrors": []}}})


def settings(**over) -> Settings:
    return Settings(shopify_shop_domain=SHOP, shopify_client_id="cid", shopify_client_secret="csecret", **over)


def test_a_token_is_fetched_once_and_reused():
    fake = FakeShopify()
    admin = ShopifyAdmin(settings(), fake.transport)
    assert admin.enabled
    assert admin.add_order_tags("1001", ["jasmin-print-ready"]) and admin.add_order_tags("1002", ["jasmin-shipped"])
    assert fake.tokens == 1 and admin.scopes == ["read_orders", "write_orders"]
    assert fake.calls[0]["variables"] == {"id": "gid://shopify/Order/1001", "tags": ["jasmin-print-ready"]}


def test_a_token_that_stopped_working_is_replaced_once():
    fake = FakeShopify()
    admin = ShopifyAdmin(settings(), fake.transport)
    assert admin.add_order_tags("1001", ["a"])
    fake.expire_next = True
    assert admin.add_order_tags("1002", ["b"])
    assert fake.tokens == 2 and len(fake.calls) == 2


def test_the_connection_test_names_the_store_or_says_what_is_wrong():
    fake = FakeShopify()
    assert ShopifyAdmin(settings(), fake.transport).check() == {
        "ok": True, "shop": "Jasmin Prints", "domain": SHOP, "scopes": ["read_orders", "write_orders"], "missingScopes": [],
    }
    fake.scope = "read_orders"
    assert ShopifyAdmin(settings(), fake.transport).check()["missingScopes"] == ["write_orders"]
    fake.reject_credentials = True
    refused = ShopifyAdmin(settings(), fake.transport).check()
    assert refused["ok"] is False and "refused the app's credentials" in refused["error"]
    assert ShopifyAdmin(Settings(), fake.transport).check() == {"ok": False, "error": "No store is set (SHOPIFY_SHOP_DOMAIN)."}
    assert ShopifyAdmin(Settings(shopify_shop_domain=SHOP), fake.transport).check()["error"] == "The app's client id and secret are not set."


def test_tagging_fails_quietly_when_shopify_refuses():
    fake = FakeShopify()
    fake.reject_credentials = True
    assert ShopifyAdmin(settings(), fake.transport).add_order_tags("1001", ["a"]) is False
    assert ShopifyAdmin(Settings(), fake.transport).add_order_tags("1001", ["a"]) is False


def test_a_legacy_token_still_works_without_asking_for_one():
    fake = FakeShopify()
    fake.tokens = 1  # the fake accepts "token-1"
    admin = ShopifyAdmin(Settings(shopify_shop_domain=SHOP, shopify_admin_token="token-1"), fake.transport)
    assert admin.add_order_tags("1001", ["a"]) and fake.tokens == 1


def test_app_webhooks_are_checked_against_the_client_secret(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data", shopify_shop_domain=SHOP, shopify_client_id="cid", shopify_client_secret="csecret"))
    client = TestClient(app)
    body = json.dumps({"id": 1, "line_items": []}).encode()
    headers = {"X-Shopify-Shop-Domain": SHOP, "Content-Type": "application/json"}
    assert client.post("/webhooks/shopify/orders-create", content=body, headers={**headers, "X-Shopify-Hmac-Sha256": sign(body, "csecret")}).status_code == 200
    assert client.post("/webhooks/shopify/orders-create", content=body, headers={**headers, "X-Shopify-Hmac-Sha256": sign(body, "local-demo-secret")}).status_code == 401
    other = {**headers, "X-Shopify-Shop-Domain": "someone-else.myshopify.com", "X-Shopify-Hmac-Sha256": sign(body, "csecret")}
    assert client.post("/webhooks/shopify/orders-create", content=body, headers=other).status_code == 403
    app.state.services.db.close()


def test_the_setup_page_gets_the_theme_editor_link_and_the_test(tmp_path):
    fake = FakeShopify()
    app = create_app(Settings(data_dir=tmp_path / "data", shopify_shop_domain=SHOP, shopify_client_id="cid", shopify_client_secret="csecret"), shopify_transport=fake.transport)
    admin = TestClient(app)
    assert admin.post("/admin/api/shopify/check").status_code == 401
    admin.auth = ("admin", "jasmin")
    info = admin.get("/admin/api/shopify").json()
    assert info["appConfigured"] and info["adminApiConfigured"] and info["webhookSecretConfigured"]
    assert info["addBlockUrl"] == f"https://{SHOP}/admin/themes/current/editor?template=product&addAppBlockId=cid/personalize&target=mainSection"
    assert admin.post("/admin/api/shopify/check").json()["shop"] == "Jasmin Prints"
    app.state.services.db.close()


def test_without_a_store_the_setup_page_has_no_link(admin):
    info = admin.get("/admin/api/shopify").json()
    assert info["addBlockUrl"] is None and info["appConfigured"] is False and info["adminApiConfigured"] is False
    assert admin.post("/admin/api/shopify/check").json()["ok"] is False


def test_the_theme_block_is_well_formed_and_its_script_is_the_apps_script():
    liquid = (EXT / "blocks" / "personalize.liquid").read_text()
    schema = json.loads(re.search(r"{% schema %}(.*){% endschema %}", liquid, re.S)[1])
    assert schema["target"] == "section" and schema["enabled_on"] == {"templates": ["product"]}
    assert len(schema["name"]) <= 25 and schema["javascript"] == "jasmin-personalize.js"
    assert {s["id"] for s in schema["settings"]} == {"product", "button_label", "hide_add_to_cart", "studio_url"}
    assert next(s for s in schema["settings"] if s["id"] == "product")["autofill"] is True
    # The handle in the theme editor link is the block's file name.
    assert add_block_url(SHOP, "cid").split("addAppBlockId=cid/")[1].startswith("personalize&")
    # The element the script looks for, with the attributes it reads.
    for attr in ("data-app-url", "data-product-handle", "data-variant-id", "data-button-label", "data-hide-add-to-cart"):
        assert attr in liquid
    assert 'class="jasmin-personalize"' in liquid
    assert (EXT / "assets" / "jasmin-personalize.js").read_bytes() == (ROOT / "static" / "shopify" / "jasmin-personalize.js").read_bytes()
    assert 'type = "theme"' in (EXT / "shopify.extension.toml").read_text()


def test_the_app_configuration_points_at_the_studio():
    import tomllib

    config = tomllib.loads((ROOT / "shopify.app.toml").read_text())
    assert config["embedded"] is False and config["access_scopes"]["scopes"] == "read_orders,write_orders"
    sub = config["webhooks"]["subscriptions"][0]
    assert sub["topics"] == ["orders/create"] and sub["uri"] == config["application_url"] + "/webhooks/shopify/orders-create"
    assert config["webhooks"]["api_version"] == Settings().shopify_api_version
    assert config["client_id"] == ""  # filled in for the store's own app; never a secret
