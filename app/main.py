"""HTTP application (``create_app``); the ASGI entry point is ``app.asgi:app``.

Public:  /editor/{template}, /api/..., /fonts/..., /assets/...
Admin:   /admin (HTTP Basic auth), /admin/api/...
Shopify: /webhooks/shopify/orders-create, /static/shopify/jasmin-personalize.js
Demo:    /demo-store (a mock Shopify storefront for local end-to-end testing)
"""
from __future__ import annotations

import json
import logging
import re
import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from . import demo_store, shopify
from .config import ROOT, Settings, load_settings
from .fonts import FontError
from .services import Invalid, NotFound, Services

STATIC = ROOT / "static"
log = logging.getLogger("jasmin")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    svc = Services(settings)
    shop_admin = shopify.ShopifyAdmin(settings)
    app = FastAPI(title="Jasmin PDF", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = svc
    app.state.settings = settings
    app.add_middleware(GZipMiddleware, minimum_size=2048)

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Invalid)
    async def invalid(_: Request, exc: Invalid):
        return JSONResponse({"detail": str(exc), "errors": exc.errors}, status_code=422)

    @app.exception_handler(FontError)
    async def font_error(_: Request, exc: FontError):
        return JSONResponse({"detail": str(exc), "errors": [str(exc)]}, status_code=422)

    basic = HTTPBasic(realm="Jasmin admin")

    def require_admin(creds: HTTPBasicCredentials = Depends(basic)) -> None:
        ok_user = secrets.compare_digest(creds.username.encode(), settings.admin_user.encode())
        ok_pass = secrets.compare_digest(creds.password.encode(), settings.admin_password.encode())
        if not (ok_user and ok_pass):
            raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": 'Basic realm="Jasmin admin"'})

    def frame_headers() -> dict[str, str]:
        return {"Content-Security-Policy": "frame-ancestors " + " ".join(settings.frame_ancestors)}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.mount("/assets/templates", StaticFiles(directory=svc.dirs["templates"]), name="template-assets")

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

    @app.get("/admin", include_in_schema=False, dependencies=[Depends(require_admin)])
    def admin_page():
        return FileResponse(STATIC / "admin.html")

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
        if not path.exists():
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
        if not path.exists():
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
        return FileResponse(path, media_type="application/pdf", filename=path.name)

    @app.get("/admin/api/shopify", dependencies=admin)
    def shopify_settings():
        return {
            "appUrl": settings.app_url,
            "webhookUrl": f"{settings.app_url}/webhooks/shopify/orders-create",
            "shopDomain": settings.shopify_shop_domain,
            "webhookSecretConfigured": bool(settings.shopify_webhook_secret),
            "adminApiConfigured": shop_admin.enabled,
            "frameAncestors": settings.frame_ancestors,
            "snippet": shopify.liquid_snippet(settings.app_url),
            "demoStore": settings.demo_store,
        }

    if settings.demo_store:
        demo_store.register(app, svc, settings)

    return app

