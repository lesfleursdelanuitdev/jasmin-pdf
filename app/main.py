"""HTTP application (``create_app``); the ASGI entry point is ``app.asgi:app``.

This module makes the parts and puts them together; the routes are in ``app/routes``.

Public:  /editor/{template}, /api/..., /fonts/..., /assets/...
Admin:   /admin (Supabase sign-in at /admin/login, or HTTP Basic auth), /admin/api/...
Shopify: /webhooks/shopify/orders-create, /static/shopify/jasmin-personalize.js
Demo:    /demo-store (a mock Shopify storefront for local end-to-end testing)
"""
from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import demo_store, shopify
from .auth import AccountError, AdminAuth, SignInUnavailable
from .backup import Backups, Scheduler
from .config import Settings, describe, load_settings
from .fonts import FontError
from .google_fonts import GoogleFonts, GoogleFontsError
from .jobs import Jobs
from .routes import STATIC, Studio, admin, pages, public, webhooks
from .services import Invalid, NotFound, Services
from .storage import Missing, Storage, StorageError

log = logging.getLogger("jasmin")


class TemplateAssets(StaticFiles):
    """/assets/templates from the data directory, fetching a file from storage first if it is not there."""

    def __init__(self, *, directory, storage: Storage):
        super().__init__(directory=directory)
        self.storage = storage

    def lookup_path(self, path: str):
        self.storage.ensure(f"templates/{path}")
        return super().lookup_path(path)


def check_settings(settings: Settings) -> None:
    modes, warnings, errors = describe(settings)
    log.info("starting with: %s", modes)
    for message in warnings:
        log.warning("settings: %s", message)
    for message in errors:
        log.error("settings: %s", message)
    if errors and settings.strict:
        # Better not to start than to run open to the world.
        raise RuntimeError("Jasmin Studio will not start with these settings: " + " ".join(errors))


def error_handlers(app: FastAPI) -> None:
    """What the app's own errors look like to a browser."""

    def answer(status: int, detail: str, errors: list[str] | None = None) -> JSONResponse:
        return JSONResponse({"detail": detail} if errors is None else {"detail": detail, "errors": errors}, status_code=status)

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound):
        return answer(404, str(exc))

    @app.exception_handler(Missing)
    async def missing(_: Request, exc: Missing):
        return answer(404, "not found")

    @app.exception_handler(Invalid)
    async def invalid(_: Request, exc: Invalid):
        return answer(422, str(exc), exc.errors)

    @app.exception_handler(FontError)
    async def font_error(_: Request, exc: FontError):
        return answer(422, str(exc), [str(exc)])

    @app.exception_handler(AccountError)
    async def account_error(_: Request, exc: AccountError):
        return answer(422, str(exc), [str(exc)])

    @app.exception_handler(GoogleFontsError)
    async def google_fonts_error(_: Request, exc: GoogleFontsError):
        return answer(502, str(exc))

    @app.exception_handler(StorageError)
    async def storage_error(_: Request, exc: StorageError):
        log.error("file storage: %s", exc)
        return answer(503, "File storage is not available right now. Please try again.")

    @app.exception_handler(SignInUnavailable)
    async def sign_in_unavailable(_: Request, exc: SignInUnavailable):
        log.warning("Supabase Auth unavailable: %s", exc)
        return answer(503, "The sign-in service cannot be reached right now. Try again in a moment.")


def create_app(
    settings: Settings | None = None,
    storage: Storage | None = None,
    auth_transport: httpx.BaseTransport | None = None,
    shopify_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    check_settings(settings)
    svc = Services(settings, storage)
    auth = AdminAuth(settings, auth_transport, svc.db)
    shop_admin = shopify.ShopifyAdmin(settings, shopify_transport)
    backups = Backups(svc, settings.backup_dir or settings.data_dir / "backups", settings.backup_keep)
    housekeeping = (lambda: svc.cleanup(settings.retention_days)) if settings.retention_days else None
    schedule = Scheduler(backups, settings.backup_hour, housekeeping) if settings.backup_auto else None

    def print_order(payload: dict) -> None:
        """The "print" job: make an order's print files, then tell Shopify they are ready."""
        order = svc.make_print_files(payload["order_id"])
        if order["items"]:
            shop_admin.add_order_tags(order["id"], ["jasmin-print-ready"])

    jobs = Jobs(svc.db, {"print": print_order}, inline=settings.jobs_inline)
    studio = Studio(settings, svc, auth, shop_admin, backups, schedule, jobs, GoogleFonts())

    app = FastAPI(title="Jasmin PDF", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.studio = studio
    app.state.services = svc
    app.state.settings = settings
    app.state.auth = auth
    app.state.shop_admin = shop_admin
    app.state.backups = backups
    app.state.jobs = jobs
    app.add_middleware(GZipMiddleware, minimum_size=2048)
    error_handlers(app)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.mount("/assets/templates", TemplateAssets(directory=svc.storage.folder("templates"), storage=svc.storage), name="template-assets")
    for part in (pages, public, webhooks, admin):
        app.include_router(part.router(studio))
    if settings.demo_store:
        demo_store.register(app, svc, settings)

    # The work that goes on beside the requests starts last, when everything it needs is there.
    if schedule:
        schedule.start()
    if not jobs.inline:
        # What the last run left unfinished: jobs that were running when it stopped, and
        # orders from before jobs existed whose print files were never made.
        jobs.recover()
        for order_id in svc.pending_orders():
            jobs.enqueue("print", f"print:{order_id}", {"order_id": order_id})
        jobs.start()
    return app
