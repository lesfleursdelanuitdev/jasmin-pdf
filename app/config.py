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
    # Postgres instead of the SQLite file in data_dir. The app's own traffic uses
    # database_url (may be a pooled address); the schema is created over
    # database_url_unpooled (a direct connection) when that is set.
    database_url: str = ""
    database_url_unpooled: str = ""
    # Supabase Storage as the store of record for the files in data_dir (see
    # app/storage.py). Used only when both the URL and the secret key are set.
    supabase_url: str = ""
    supabase_secret_key: str = ""
    storage_bucket: str = "jasmin"
    # Admin sign-in with Supabase Auth (see app/auth.py), used when the URL, the
    # publishable key and at least one admin address are set. Otherwise the admin
    # is behind HTTP Basic with admin_user / admin_password.
    supabase_publishable_key: str = ""
    admin_emails: list[str] = field(default_factory=list)
    session_secret: str = ""
    # Public base URL of this app (used in Shopify snippets and webhooks).
    app_url: str = "http://localhost:8000"
    admin_user: str = "admin"
    admin_password: str = "jasmin"
    # Shopify
    shopify_shop_domain: str = ""  # e.g. jasmin-prints.myshopify.com
    shopify_webhook_secret: str = ""
    shopify_admin_token: str = ""  # a legacy custom app's token; an app from the Dev Dashboard uses the two below
    # The Jasmin app in the store's Dev Dashboard (see shopify.app.toml). With these the
    # app asks Shopify for its own Admin API token, and webhooks are checked against the secret.
    shopify_client_id: str = ""
    shopify_client_secret: str = ""
    shopify_api_version: str = "2026-07"
    # Extra storefront origins allowed to embed the editor (custom domains).
    storefront_origins: list[str] = field(default_factory=list)
    # The /demo-store mock storefront (simulates Shopify locally).
    demo_store: bool = True
    # Print files: maximum embedded photo resolution.
    print_max_dpi: int = 300
    # Backups: one zip per backup (database rows and every stored file). The folder
    # defaults to <data_dir>/backups; keep the newest ``backup_keep``. With
    # ``backup_auto`` one is made every day at ``backup_hour`` (UTC).
    backup_dir: Path | None = None
    backup_keep: int = 14
    backup_auto: bool = False
    # Jobs (print files) run at once in the caller's thread; a real run uses the worker instead (app/jobs.py).
    jobs_inline: bool = True
    # Photos and drafts that never became an approved design are removed after this many
    # days (0: never). Done once a day, before the backup.
    retention_days: int = 0
    # Refuse to start with settings that would leave the app open (see ``describe``). On for a
    # real run, which reads its settings from the environment; off for settings made in code.
    strict: bool = False
    backup_hour: int = 3

    @property
    def webhook_secret(self) -> str:
        # The demo store signs its fake webhooks with this same secret, so a
        # local default keeps the end-to-end demo working out of the box.
        # Webhooks an app subscribes to in its configuration are signed with the app's client secret.
        return self.webhook_secrets[0]

    @property
    def webhook_secrets(self) -> list[str]:
        """Every secret an incoming webhook may be signed with; the first one signs the demo store's own."""
        # Both may be set: a webhook made by hand in the store's settings has its own secret, one the
        # app subscribes to is signed with the app's client secret.
        secrets_ = [s for s in (self.shopify_webhook_secret, self.shopify_client_secret) if s]
        return secrets_ or ["local-demo-secret"]

    @property
    def frame_ancestors(self) -> list[str]:
        origins = ["'self'", "https://*.myshopify.com"]
        if self.shopify_shop_domain:
            origins.append(f"https://{self.shopify_shop_domain}")
        origins.extend(self.storefront_origins)
        return origins


def describe(s: Settings) -> tuple[str, list[str], list[str]]:
    """What this configuration amounts to: one line naming the modes, the warnings, and the errors that must stop the app."""
    supabase_auth = bool(s.supabase_url and s.supabase_publishable_key and s.admin_emails)
    supabase_files = bool(s.supabase_url and s.supabase_secret_key)
    store = bool(s.shopify_shop_domain and (s.shopify_admin_token or (s.shopify_client_id and s.shopify_client_secret)))
    modes = (
        f"database {'Postgres' if s.database_url else 'SQLite'}, files {'Supabase Storage' if supabase_files else 'data directory only'}, "
        f"admin sign-in {'Supabase' if supabase_auth else 'HTTP Basic'}, Shopify store {'connected' if store else 'not connected'}, "
        f"demo store {'on' if s.demo_store else 'off'}, backups {'daily' if s.backup_auto else 'by hand'}, "
        f"housekeeping {f'after {s.retention_days} days' if s.retention_days else 'off'}"
    )
    public = s.app_url.startswith("https://")
    warnings: list[str] = []
    errors: list[str] = []
    if not supabase_auth and (s.supabase_url or s.supabase_publishable_key or s.admin_emails):
        missing = [n for n, v in (("SUPABASE_URL", s.supabase_url), ("SUPABASE_PUBLISHABLE_KEY", s.supabase_publishable_key), ("ADMIN_EMAILS", s.admin_emails)) if not v]
        errors.append(f"Supabase sign-in is half set up ({', '.join(missing)} missing): the admin would fall back to the shared password.")
    if supabase_auth and not s.supabase_secret_key:
        warnings.append("SUPABASE_SECRET_KEY is not set: admins cannot change passwords or add admins.")
    if supabase_auth and not s.session_secret:
        warnings.append("SESSION_SECRET is not set: admin sign-ins will not survive a restart.")
    if public and not supabase_auth and s.admin_password == Settings.admin_password:
        errors.append("APP_URL is public and the admin still has the default password: set ADMIN_PASSWORD or set up Supabase sign-in.")
    if supabase_files and not s.database_url:
        warnings.append("Files are in Supabase Storage but the database is a SQLite file in the data directory: losing that directory loses the database.")
    if s.demo_store and store:
        warnings.append("The demo store is on although a Shopify store is connected: anyone can place fake orders in it. Set DEMO_STORE=false.")
    if public and not s.demo_store and not (s.shopify_webhook_secret or s.shopify_client_secret):
        warnings.append("No webhook secret is set: orders from Shopify cannot be verified and will be refused.")
    if bool(s.shopify_client_id) != bool(s.shopify_client_secret):
        warnings.append("Only one of SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET is set.")
    if store and not s.storefront_origins:
        warnings.append("STOREFRONT_ORIGINS is not set: the editor can only be opened from the store's myshopify.com address.")
    return modes, warnings, errors


def load_settings() -> Settings:
    env = os.environ
    s = Settings()
    s.strict = True
    if env.get("JASMIN_DATA_DIR"):
        s.data_dir = Path(env["JASMIN_DATA_DIR"]).resolve()
    s.database_url = env.get("DATABASE_URL", "").strip()
    s.database_url_unpooled = env.get("DATABASE_URL_UNPOOLED", "").strip()
    s.supabase_url = env.get("SUPABASE_URL", "").strip().rstrip("/")
    s.supabase_secret_key = (env.get("SUPABASE_SECRET_KEY") or env.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    s.storage_bucket = env.get("SUPABASE_STORAGE_BUCKET", "").strip() or s.storage_bucket
    s.supabase_publishable_key = (env.get("SUPABASE_PUBLISHABLE_KEY") or env.get("SUPABASE_ANON_KEY") or "").strip()
    s.admin_emails = [e.strip().lower() for e in env.get("ADMIN_EMAILS", "").split(",") if e.strip()]
    s.session_secret = env.get("SESSION_SECRET", "").strip()
    s.app_url = env.get("APP_URL", s.app_url).rstrip("/")
    s.admin_user = env.get("ADMIN_USER", s.admin_user)
    s.admin_password = env.get("ADMIN_PASSWORD", s.admin_password)
    s.shopify_shop_domain = env.get("SHOPIFY_SHOP_DOMAIN", "").strip()
    s.shopify_webhook_secret = env.get("SHOPIFY_WEBHOOK_SECRET", "").strip()
    s.shopify_admin_token = env.get("SHOPIFY_ADMIN_TOKEN", "").strip()
    s.shopify_client_id = env.get("SHOPIFY_CLIENT_ID", "").strip()
    s.shopify_client_secret = env.get("SHOPIFY_CLIENT_SECRET", "").strip()
    s.shopify_api_version = env.get("SHOPIFY_API_VERSION", s.shopify_api_version)
    s.storefront_origins = [
        o.strip().rstrip("/") for o in env.get("STOREFRONT_ORIGINS", "").split(",") if o.strip()
    ]
    s.demo_store = _bool(env.get("DEMO_STORE"), True)
    if env.get("PRINT_MAX_DPI"):
        s.print_max_dpi = int(env["PRINT_MAX_DPI"])
    if env.get("BACKUP_DIR"):
        s.backup_dir = Path(env["BACKUP_DIR"]).resolve()
    if env.get("BACKUP_KEEP"):
        s.backup_keep = max(1, int(env["BACKUP_KEEP"]))
    s.backup_auto = _bool(env.get("BACKUP_AUTO"), False)
    s.jobs_inline = False
    if env.get("RETENTION_DAYS"):
        s.retention_days = max(0, int(env["RETENTION_DAYS"]))
    if env.get("BACKUP_HOUR_UTC"):
        s.backup_hour = min(max(int(env["BACKUP_HOUR_UTC"]), 0), 23)
    return s
