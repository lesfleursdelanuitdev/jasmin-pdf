"""/admin/api: everything here needs an admin."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import google_fonts as google_fonts_module
from .. import shopify
from ..auth import MIN_PASSWORD
from ..services import NotFound
from . import Studio


def router(studio: Studio) -> APIRouter:
    r = APIRouter(prefix="/admin/api", dependencies=[Depends(studio.require_admin)])
    settings, svc, auth = studio.settings, studio.svc, studio.auth
    shop_admin, backups, schedule = studio.shop_admin, studio.backups, studio.schedule

    @r.get("/me")
    def me(request: Request):
        return {"email": studio.signed_in_as(request), "demoStore": settings.demo_store}

    # Accounts (only with Supabase sign-in): one's own password, and who else is an admin.
    def signed_in(request: Request) -> str:
        email = studio.signed_in_as(request)
        if email is None:
            raise HTTPException(404, "Accounts are managed with Supabase sign-in, which is not set up here.")
        return email

    def account_out() -> dict:
        return {"admins": auth.admins(), "minPassword": MIN_PASSWORD}

    @r.get("/account")
    def account(request: Request):
        return {"email": signed_in(request), **account_out()}

    @r.post("/account/password")
    async def change_password(request: Request):
        email = signed_in(request)
        if auth.throttled(f"password:{email}"):
            return JSONResponse({"detail": "Too many attempts. Wait a few minutes and try again."}, status_code=429)
        body = await request.json()
        await run_in_threadpool(auth.change_password, email, str(body.get("current", "")), str(body.get("new", "")))
        return {"ok": True}

    @r.post("/admins")
    async def add_admin(request: Request):
        email = signed_in(request)
        body = await request.json()
        await run_in_threadpool(auth.add_admin, str(body.get("email", "")), str(body.get("password", "")), email)
        return await run_in_threadpool(account_out)

    @r.delete("/admins/{address}")
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
            "assets": svc.assets_url(t),
            "definition": t["definition"],
            "editorUrl": f"/editor/{t['id']}",
        }

    @r.get("/templates")
    def list_templates():
        return [template_admin(t) for t in svc.list_templates()]

    @r.post("/templates")
    async def create_template(name: str = Form(...), file: UploadFile = File(...)):
        data = await file.read()
        return template_admin(await run_in_threadpool(svc.create_template, name.strip() or "Untitled", data))

    @r.get("/templates/{template_id}")
    def admin_get_template(template_id: str):
        return template_admin(svc.get_template(template_id))

    @r.patch("/templates/{template_id}")
    async def admin_update_template(template_id: str, request: Request):
        return template_admin(await run_in_threadpool(svc.update_template, template_id, await request.json()))

    @r.delete("/templates/{template_id}")
    def admin_delete_template(template_id: str):
        svc.delete_template(template_id)
        return {"ok": True}

    @r.get("/templates/{template_id}/source.pdf")
    def admin_template_pdf(template_id: str):
        t = svc.get_template(template_id)
        return FileResponse(svc.pdf_path(t), media_type="application/pdf", filename=f"{t['id']}.pdf")

    @r.get("/fonts")
    def list_fonts():
        return [f.public() for f in svc.fonts.all()]

    @r.post("/fonts")
    async def upload_font(file: UploadFile = File(...)):
        return await run_in_threadpool(svc.add_font, file.filename or "font.ttf", await file.read())

    # Google Fonts: search the catalogue, and put the chosen styles of a family in the studio.
    @r.get("/google-fonts")
    def google_fonts(q: str = "", category: str = ""):
        have = {(f.family.lower(), f.style.replace(" ", "").lower()) for f in svc.fonts.all()}
        found = studio.google.search(q[:80], category[:40], limit=40)
        for family in found["families"]:
            for style in family["styles"]:
                style["installed"] = (family["family"].lower(), style["label"].replace(" ", "").lower()) in have
        return {**found, "categories": studio.google.categories()}

    @r.post("/google-fonts")
    async def import_google_font(request: Request):
        body = await request.json()
        family, styles = str(body.get("family", "")), [str(s) for s in body.get("styles") or []]
        if not styles or len(styles) > google_fonts_module.MAX_STYLES:
            raise HTTPException(422, f"Choose between 1 and {google_fonts_module.MAX_STYLES} styles.")

        def fetch() -> dict:
            files = [(f"{family} {google_fonts_module.style_label(s)}", studio.google.download(family, s)) for s in dict.fromkeys(styles)]
            return svc.import_fonts(files)

        return await run_in_threadpool(fetch)

    @r.get("/orders")
    def list_orders():
        return svc.list_orders()

    @r.get("/orders/{order_id}")
    def get_order(order_id: str):
        return svc.get_order(order_id)

    @r.post("/orders/{order_id}/status")
    async def order_status(order_id: str, request: Request):
        status = (await request.json()).get("status", "")

        def change() -> dict:
            order = svc.set_order_status(order_id, status)
            if status in ("in_production", "shipped"):
                shop_admin.add_order_tags(order_id, [f"jasmin-{status.replace('_', '-')}"])
            return order

        return await run_in_threadpool(change)

    @r.post("/order-items/{item_id}/regenerate")
    def regenerate(item_id: int):
        return svc.generate_print_file(item_id)

    @r.get("/order-items/{item_id}/print.pdf")
    def print_file(item_id: int):
        path = svc.print_file(item_id)
        return FileResponse(path, media_type="application/pdf", filename=path.name)

    @r.get("/backups")
    def list_backups():
        return {
            "running": backups.running,
            "error": backups.error,
            "automatic": bool(schedule),
            "hourUtc": settings.backup_hour,
            "keep": backups.keep,
            "retentionDays": settings.retention_days if schedule else 0,
            "backups": backups.list(),
        }

    @r.post("/backups")
    def start_backup():
        # Made in the background: with many files it takes longer than a request should.
        if not backups.start("manual"):
            return JSONResponse({"detail": "A backup is already being made."}, status_code=409)
        return JSONResponse({"started": True}, status_code=202)

    @r.get("/backups/{name}")
    def download_backup(name: str):
        path = backups.path(name)
        if path is None:
            raise NotFound("backup not found")
        return FileResponse(path, media_type="application/zip", filename=name)

    @r.get("/shopify")
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

    @r.post("/shopify/check")
    def shopify_check():
        return shop_admin.check()

    return r
