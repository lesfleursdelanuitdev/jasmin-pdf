"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = ROOT / "data"
    # Public base URL of this app (used in Shopify snippets and webhooks).
    app_url: str = "http://localhost:8000"
    admin_user: str = "admin"
    admin_password: str = "jasmin"
    # Shopify
    shopify_shop_domain: str = ""  # e.g. jasmin-prints.myshopify.com
    shopify_webhook_secret: str = ""
    shopify_admin_token: str = ""  # optional, enables order tagging
    shopify_api_version: str = "2025-07"
    # Extra storefront origins allowed to embed the editor (custom domains).
    storefront_origins: list[str] = field(default_factory=list)
    # The /demo-store mock storefront (simulates Shopify locally).
    demo_store: bool = True
    # Print files: maximum embedded photo resolution.
    print_max_dpi: int = 300

    @property
    def webhook_secret(self) -> str:
        # The demo store signs its fake webhooks with this same secret, so a
        # local default keeps the end-to-end demo working out of the box.
        return self.shopify_webhook_secret or "local-demo-secret"

    @property
    def frame_ancestors(self) -> list[str]:
        origins = ["'self'", "https://*.myshopify.com"]
        if self.shopify_shop_domain:
            origins.append(f"https://{self.shopify_shop_domain}")
        origins.extend(self.storefront_origins)
        return origins


def load_settings() -> Settings:
    env = os.environ
    s = Settings()
    if env.get("JASMIN_DATA_DIR"):
        s.data_dir = Path(env["JASMIN_DATA_DIR"]).resolve()
    s.app_url = env.get("APP_URL", s.app_url).rstrip("/")
    s.admin_user = env.get("ADMIN_USER", s.admin_user)
    s.admin_password = env.get("ADMIN_PASSWORD", s.admin_password)
    s.shopify_shop_domain = env.get("SHOPIFY_SHOP_DOMAIN", "").strip()
    s.shopify_webhook_secret = env.get("SHOPIFY_WEBHOOK_SECRET", "").strip()
    s.shopify_admin_token = env.get("SHOPIFY_ADMIN_TOKEN", "").strip()
    s.shopify_api_version = env.get("SHOPIFY_API_VERSION", s.shopify_api_version)
    s.storefront_origins = [
        o.strip().rstrip("/") for o in env.get("STOREFRONT_ORIGINS", "").split(",") if o.strip()
    ]
    s.demo_store = _bool(env.get("DEMO_STORE"), True)
    if env.get("PRINT_MAX_DPI"):
        s.print_max_dpi = int(env["PRINT_MAX_DPI"])
    return s
