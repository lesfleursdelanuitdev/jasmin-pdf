"""HTTP application (``create_app``); the ASGI entry point is ``app.asgi:app``.

Public:  /editor/{template}, /api/..., /fonts/..., /assets/...
Admin:   /admin (Supabase sign-in at /admin/login, or HTTP Basic auth), /admin/api/...
Shopify: /webhooks/shopify/orders-create, /static/shopify/jasmin-personalize.js
Demo:    /demo-store (a mock Shopify storefront for local end-to-end testing)
"""
from __future__ import annotations

import html
import json
import logging
import re
import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

import httpx

from . import demo_store, shopify
from .auth import COOKIE, MIN_PASSWORD, SESSION_SECONDS, AccountError, AdminAuth, SignInRefused, SignInUnavailable
from .backup import Backups, Scheduler
from .config import ROOT, Settings, load_settings
from .fonts import FontError
from .services import Invalid, NotFound, Services
from .storage import Storage, StorageError

STATIC = ROOT / "static"
log = logging.getLogger("jasmin")


class TemplateAssets(StaticFiles):
    """/assets/templates from the data directory, fetching a file from storage first if it is not there."""

    def __init__(self, *, directory, storage: Storage):
        super().__init__(directory=directory)
        self.storage = storage

    def lookup_path(self, path: str):
        self.storage.ensure(f"templates/{path}")
        return super().lookup_path(path)


def create_app(
    settings: Settings | None = None,
    storage: Storage | None = None,
    auth_transport: httpx.BaseTransport | None = None,
    shopify_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    svc = Services(settings, storage)
    auth = AdminAuth(settings, auth_transport, svc.db)
    shop_admin = shopify.ShopifyAdmin(settings, shopify_transport)
    app = FastAPI(title="Jasmin PDF", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = svc
    app.state.settings = settings
    app.state.auth = auth
    app.state.shop_admin = shop_admin
    backups = Backups(svc, settings.backup_dir or settings.data_dir / "backups", settings.backup_keep)
    app.state.backups = backups
    schedule = Scheduler(backups, settings.backup_hour) if settings.backup_auto else None
    if schedule:
        schedule.start()
    app.add_middleware(GZipMiddleware, minimum_size=2048)

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Invalid)
    async def invalid(_: Request, exc: Invalid):
        return JSONResponse({"detail": str(exc), "errors": exc.errors}, status_code=422)

    @app.exception_handler(StorageError)
    async def storage_error(_: Request, exc: StorageError):
        log.error("file storage: %s", exc)
        return JSONResponse({"detail": "File storage is not available right now. Please try again."}, status_code=503)

    @app.exception_handler(FontError)
    async def font_error(_: Request, exc: FontError):
        return JSONResponse({"detail": str(exc), "errors": [str(exc)]}, status_code=422)

    basic = HTTPBasic(realm="Jasmin admin")

    def require_basic(creds: HTTPBasicCredentials = Depends(basic)) -> None:
        ok_user = secrets.compare_digest(creds.username.encode(), settings.admin_user.encode())
        ok_pass = secrets.compare_digest(creds.password.encode(), settings.admin_password.encode())
        if not (ok_user and ok_pass):
            raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": 'Basic realm="Jasmin admin"'})

    def own_origin(request: Request) -> None:
        # A browser sends Origin with every cross-site POST, PATCH, PUT and DELETE.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not auth.same_origin(
            request.headers.get("origin"), request.headers.get("host")
        ):
            raise HTTPException(403, "Request from another site refused")

    def require_session(request: Request) -> None:
        own_origin(request)
        if auth.read(request.cookies.get(COOKIE)) is None:
            raise HTTPException(401, "Sign in required")

    require_admin = require_session if auth.enabled else require_basic

    def frame_headers() -> dict[str, str]:
        return {"Content-Security-Policy": "frame-ancestors " + " ".join(settings.frame_ancestors)}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.mount("/assets/templates", TemplateAssets(directory=svc.dirs["templates"], storage=svc.storage), name="template-assets")

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/demo-store" if settings.demo_store else "/admin")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")

    @app.get("/editor/{template_id}", include_in_schema=False)
    def editor_page(template_id: str):
        return FileResponse(STATIC / "editor.html", headers=frame_headers())

    if auth.enabled:

        @app.get("/admin", include_in_schema=False)
        def admin_page(request: Request):
            if auth.read(request.cookies.get(COOKIE)) is None:
                return RedirectResponse("/admin/login", status_code=303)
            return FileResponse(STATIC / "admin.html", headers={"Cache-Control": "no-store"})

        def login_page(message: str = "", email: str = "", status: int = 200) -> HTMLResponse:
            page = (STATIC / "login.html").read_text(encoding="utf-8")
            notice = f'<p class="notice notice-bad" role="alert">{html.escape(message)}</p>' if message else ""
            page = page.replace("<!--message-->", notice).replace("{{email}}", html.escape(email, quote=True))
            return HTMLResponse(page, status_code=status, headers={"Cache-Control": "no-store"})

        @app.get("/admin/login", include_in_schema=False)
        def login_form(request: Request):
            if auth.read(request.cookies.get(COOKIE)) is not None:
                return RedirectResponse("/admin", status_code=303)
            return login_page()

        @app.post("/admin/login", include_in_schema=False, dependencies=[Depends(own_origin)])
        def login(request: Request, email: str = Form(""), password: str = Form("")):
            email = email.strip()[:254]
            if auth.throttled(request.client.host if request.client else "unknown"):
                return login_page("Too many sign-in attempts. Please wait a few minutes and try again.", email, 429)
            try:
                address = auth.sign_in(email, password)
            except SignInRefused:
                return login_page("That email and password did not work.", email, 401)
            except SignInUnavailable as exc:
                log.error("admin sign-in: Supabase did not answer: %s", exc)
                return login_page("Sign-in is not available right now. Please try again in a moment.", email, 503)
            response = RedirectResponse("/admin", status_code=303)
            response.set_cookie(
                COOKIE,
                auth.issue(address),
                max_age=SESSION_SECONDS,
                path="/admin",
                httponly=True,
                samesite="lax",
                secure=auth.secure_cookie,
            )
            return response

        @app.post("/admin/logout", include_in_schema=False, dependencies=[Depends(own_origin)])
        def logout():
            response = RedirectResponse("/admin/login", status_code=303)
            response.delete_cookie(COOKIE, path="/admin", httponly=True, samesite="lax", secure=auth.secure_cookie)
            return response

    else:

        @app.get("/admin", include_in_schema=False, dependencies=[Depends(require_admin)])
        def admin_page():
            return FileResponse(STATIC / "admin.html")

        @app.get("/admin/login", include_in_schema=False)
        def login_form():
            return RedirectResponse("/admin", status_code=303)

    # ------------------------------------------------------------------
    # Public API (customer editor)
    # ------------------------------------------------------------------
    @app.get("/api/templates/{template_id}")
    def get_template(template_id: str):
        return svc.editor_payload(svc.get_template(template_id))

    @app.get("/api/storefront/products/{handle}")
    def product_template(handle: str):
        t = svc.template_for_product(handle)
        headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=60"}
        if t is None:
            return JSONResponse({"templateId": None}, headers=headers)
        return JSONResponse(
            {"templateId": t["id"], "name": t["name"], "editorUrl": f"{settings.app_url}/editor/{t['id']}"},
            headers=headers,
        )

    @app.get("/fonts/{name}.ttf", include_in_schema=False)
    def font_file(name: str):
        info = svc.fonts.get(name)
        if info is None:
            raise NotFound("font not found")
        return FileResponse(info.path, media_type="font/ttf", headers={"Cache-Control": "public, max-age=86400"})

    @app.post("/api/uploads")
    async def upload(file: UploadFile = File(...)):
        data = await file.read()
        return svc.save_upload(file.filename or "photo", data)

    @app.get("/api/uploads/{upload_id}")
    def upload_info(upload_id: str):
        return svc.upload_public(svc.get_upload(upload_id))

    @app.get("/assets/uploads/{name}", include_in_schema=False)
    def upload_preview(name: str):
        # Only the downscaled previews are public; originals stay private.
        if not re.fullmatch(r"img_[A-Za-z0-9]+-preview\.jpg", name):
            raise NotFound("not found")
        path = svc.dirs["uploads"] / name
        if not svc.storage.ensure(f"uploads/{name}"):
            raise NotFound("not found")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.post("/api/designs")
    async def create_design(request: Request):
        body = await request.json()
        d = svc.create_design(str(body.get("templateId", "")), body.get("values") or {})
        return svc.design_public(d)

    @app.get("/api/designs/{design_id}")
    def get_design(design_id: str):
        return svc.design_public(svc.get_design(design_id))

    @app.put("/api/designs/{design_id}")
    async def update_design(design_id: str, request: Request):
        body = await request.json()
        return svc.design_public(svc.update_design(design_id, body.get("values") or {}))

    @app.post("/api/designs/{design_id}/proof")
    def proof(design_id: str):
        return svc.proof(design_id)

    @app.post("/api/designs/{design_id}/approve")
    def approve(design_id: str):
        return svc.approve(design_id)

    @app.get("/api/designs/{design_id}/{filename}", include_in_schema=False)
    def design_file(design_id: str, filename: str):
        if not re.fullmatch(r"dsn_[A-Za-z0-9]+", design_id) or not re.fullmatch(
            r"(proof-p\d+\.jpg|proof\.pdf|thumbnail\.jpg)", filename
        ):
            raise NotFound("not found")
        path = svc.design_dir(design_id) / filename
        if not svc.storage.ensure(f"designs/{design_id}/{filename}"):
            raise NotFound("not found")
        if filename.endswith(".pdf"):
            return FileResponse(path, media_type="application/pdf", filename=f"{design_id}-proof.pdf")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------
    # Shopify webhook
    # ------------------------------------------------------------------
    def process_order(order_id: str) -> None:
        svc.generate_print_files(order_id)
        order = svc.get_order(order_id)
        if order["items"] and all(i["status"] == "ready" for i in order["items"]):
            shop_admin.add_order_tags(order_id, ["jasmin-print-ready"])

    app.state.process_order = process_order

    @app.post("/webhooks/shopify/orders-create")
    async def orders_create(request: Request, background: BackgroundTasks):
        body = await request.body()
        if not shopify.verify_webhook(body, request.headers.get("X-Shopify-Hmac-Sha256"), settings.webhook_secret):
            return JSONResponse({"detail": "invalid signature"}, status_code=401)
        shop = request.headers.get("X-Shopify-Shop-Domain", "")
        if settings.shopify_shop_domain and shop and shop != settings.shopify_shop_domain:
            return JSONResponse({"detail": "unexpected shop"}, status_code=403)
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"detail": "invalid JSON"}, status_code=400)
        source = "demo" if request.headers.get("X-Jasmin-Demo") == "1" else "shopify"
        order = svc.record_order(payload, source=source, shop=shop)
        if order is not None:
            background.add_task(process_order, order["id"])
        # Shopify only needs a fast 2xx; processing continues in the background.
        return {"ok": True, "personalized": order is not None}

    # ------------------------------------------------------------------
    # Admin API
    # ------------------------------------------------------------------
    admin = [Depends(require_admin)]

    @app.get("/admin/api/me", dependencies=admin)
    def me(request: Request):
        return {"email": auth.read(request.cookies.get(COOKIE))}

    # Accounts (only with Supabase sign-in): one's own password, and who else is an admin.
    def signed_in(request: Request) -> str:
        email = auth.read(request.cookies.get(COOKIE))
        if email is None:
            raise HTTPException(404, "Accounts are managed with Supabase sign-in, which is not set up here.")
        return email

    def account_out() -> dict:
        return {"admins": auth.admins(), "minPassword": MIN_PASSWORD}

    @app.exception_handler(AccountError)
    async def account_error(_: Request, exc: AccountError):
        return JSONResponse({"detail": str(exc), "errors": [str(exc)]}, status_code=422)

    @app.exception_handler(SignInUnavailable)
    async def sign_in_unavailable(_: Request, exc: SignInUnavailable):
        log.warning("Supabase Auth unavailable: %s", exc)
        return JSONResponse({"detail": "The sign-in service cannot be reached right now. Try again in a moment."}, status_code=503)

    @app.get("/admin/api/account", dependencies=admin)
    def account(request: Request):
        return {"email": signed_in(request), **account_out()}

    @app.post("/admin/api/account/password", dependencies=admin)
    async def change_password(request: Request):
        email = signed_in(request)
        if auth.throttled(f"password:{email}"):
            return JSONResponse({"detail": "Too many attempts. Wait a few minutes and try again."}, status_code=429)
        body = await request.json()
        auth.change_password(email, str(body.get("current", "")), str(body.get("new", "")))
        return {"ok": True}

    @app.post("/admin/api/admins", dependencies=admin)
    async def add_admin(request: Request):
        email = signed_in(request)
        body = await request.json()
        auth.add_admin(str(body.get("email", "")), str(body.get("password", "")), email)
        return account_out()

    @app.delete("/admin/api/admins/{address}", dependencies=admin)
    def remove_admin(address: str, request: Request):
        auth.remove_admin(address, signed_in(request))
        return account_out()


    def template_admin(t: dict) -> dict:
        return {
            "id": t["id"],
            "name": t["name"],
            "status": t["status"],
            "revision": t["revision"],
            "productHandles": t["product_handles"],
            "createdAt": t["created_at"],
            "updatedAt": t["updated_at"],
            "assets": svc.assets_url(t["id"], t["revision"]),
            "definition": t["definition"],
            "editorUrl": f"/editor/{t['id']}",
        }

    @app.get("/admin/api/templates", dependencies=admin)
    def list_templates():
        return [template_admin(t) for t in svc.list_templates()]

    @app.post("/admin/api/templates", dependencies=admin)
    async def create_template(name: str = Form(...), file: UploadFile = File(...)):
        data = await file.read()
        return template_admin(svc.create_template(name.strip() or "Untitled", data))

    @app.get("/admin/api/templates/{template_id}", dependencies=admin)
    def admin_get_template(template_id: str):
        return template_admin(svc.get_template(template_id))

    @app.patch("/admin/api/templates/{template_id}", dependencies=admin)
    async def admin_update_template(template_id: str, request: Request):
        return template_admin(svc.update_template(template_id, await request.json()))

    @app.delete("/admin/api/templates/{template_id}", dependencies=admin)
    def admin_delete_template(template_id: str):
        svc.delete_template(template_id)
        return {"ok": True}

    @app.get("/admin/api/templates/{template_id}/source.pdf", dependencies=admin)
    def admin_template_pdf(template_id: str):
        t = svc.get_template(template_id)
        return FileResponse(svc.pdf_path(t), media_type="application/pdf", filename=f"{t['id']}.pdf")

    @app.get("/admin/api/fonts", dependencies=admin)
    def list_fonts():
        return [f.public() for f in svc.fonts.all()]

    @app.post("/admin/api/fonts", dependencies=admin)
    async def upload_font(file: UploadFile = File(...)):
        info = svc.fonts.add_upload(file.filename or "font.ttf", await file.read())
        svc.storage.put(f"fonts/{info.path.name}")
        return info.public()

    @app.get("/admin/api/orders", dependencies=admin)
    def list_orders():
        return svc.list_orders()

    @app.get("/admin/api/orders/{order_id}", dependencies=admin)
    def get_order(order_id: str):
        return svc.get_order(order_id)

    @app.post("/admin/api/orders/{order_id}/status", dependencies=admin)
    async def order_status(order_id: str, request: Request):
        status = (await request.json()).get("status", "")
        order = svc.set_order_status(order_id, status)
        if status in ("in_production", "shipped"):
            shop_admin.add_order_tags(order_id, [f"jasmin-{status.replace('_', '-')}"])
        return order

    @app.post("/admin/api/order-items/{item_id}/regenerate", dependencies=admin)
    def regenerate(item_id: int):
        return svc.generate_print_file(item_id)

    @app.get("/admin/api/order-items/{item_id}/print.pdf", dependencies=admin)
    def print_file(item_id: int):
        item = svc.db.one("SELECT * FROM order_items WHERE id=?", (item_id,))
        if not item or not item["print_file"]:
            raise NotFound("print file not ready")
        path = svc.dirs["print"] / item["print_file"]
        svc.storage.ensure(f"print/{item['print_file']}")
        return FileResponse(path, media_type="application/pdf", filename=path.name)

    @app.get("/admin/api/backups", dependencies=admin)
    def list_backups():
        return {
            "running": backups.running,
            "error": backups.error,
            "automatic": bool(schedule),
            "hourUtc": settings.backup_hour,
            "keep": backups.keep,
            "backups": backups.list(),
        }

    @app.post("/admin/api/backups", dependencies=admin)
    def start_backup():
        # Made in the background: with many files it takes longer than a request should.
        if not backups.start("manual"):
            return JSONResponse({"detail": "A backup is already being made."}, status_code=409)
        return JSONResponse({"started": True}, status_code=202)

    @app.get("/admin/api/backups/{name}", dependencies=admin)
    def download_backup(name: str):
        path = backups.path(name)
        if path is None:
            raise NotFound("backup not found")
        return FileResponse(path, media_type="application/zip", filename=name)

    @app.get("/admin/api/shopify", dependencies=admin)
    def shopify_settings():
        ready = bool(settings.shopify_shop_domain and settings.shopify_client_id)
        return {
            "appUrl": settings.app_url,
            "webhookUrl": f"{settings.app_url}/webhooks/shopify/orders-create",
            "shopDomain": settings.shopify_shop_domain,
            "appConfigured": bool(settings.shopify_client_id and settings.shopify_client_secret),
            "webhookSecretConfigured": bool(settings.shopify_webhook_secret or settings.shopify_client_secret),
            "adminApiConfigured": shop_admin.enabled,
            # The theme editor, opened on the product page with the Personalize button ready to place.
            "addBlockUrl": shopify.add_block_url(settings.shopify_shop_domain, settings.shopify_client_id) if ready else None,
            "frameAncestors": settings.frame_ancestors,
            "snippet": shopify.liquid_snippet(settings.app_url),
            "demoStore": settings.demo_store,
        }

    @app.post("/admin/api/shopify/check", dependencies=admin)
    def shopify_check():
        return shop_admin.check()

    if settings.demo_store:
        demo_store.register(app, svc, settings)

    return app

