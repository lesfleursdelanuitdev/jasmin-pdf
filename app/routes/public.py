"""What the customer editor and the storefront call. No sign-in: a design is reached by its unguessable id."""
from __future__ import annotations

import re

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from ..services import NotFound
from . import Studio


def router(studio: Studio) -> APIRouter:
    r = APIRouter()
    settings, svc = studio.settings, studio.svc

    @r.get("/api/templates/{template_id}")
    def get_template(template_id: str):
        return svc.editor_payload(svc.get_template(template_id))

    @r.get("/api/storefront/products/{handle}")
    def product_template(handle: str):
        t = svc.template_for_product(handle)
        headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=60"}
        if t is None:
            return JSONResponse({"templateId": None}, headers=headers)
        return JSONResponse(
            {"templateId": t["id"], "name": t["name"], "editorUrl": f"{settings.app_url}/editor/{t['id']}"},
            headers=headers,
        )

    @r.get("/fonts/{name}.ttf", include_in_schema=False)
    def font_file(name: str):
        info = svc.fonts.get(name)
        if info is None:
            raise NotFound("font not found")
        return FileResponse(info.path, media_type="font/ttf", headers={"Cache-Control": "public, max-age=86400"})

    @r.post("/api/uploads")
    async def upload(file: UploadFile = File(...)):
        data = await file.read()
        # Decoding and storing take seconds: on a worker thread, or every other request waits.
        return await run_in_threadpool(svc.save_upload, file.filename or "photo", data)

    @r.get("/api/uploads/{upload_id}")
    def upload_info(upload_id: str):
        return svc.upload_public(svc.get_upload(upload_id))

    @r.get("/assets/uploads/{name}", include_in_schema=False)
    def upload_preview(name: str):
        # Only the downscaled previews are public; originals stay private.
        if not re.fullmatch(r"img_[A-Za-z0-9]+-preview\.jpg", name):
            raise NotFound("not found")
        path = svc.storage.path(f"uploads/{name}")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @r.post("/api/designs")
    async def create_design(request: Request):
        body = await request.json()
        d = await run_in_threadpool(svc.create_design, str(body.get("templateId", "")), body.get("values") or {})
        return svc.design_public(d)

    @r.get("/api/designs/{design_id}")
    def get_design(design_id: str):
        return svc.design_public(svc.get_design(design_id))

    @r.put("/api/designs/{design_id}")
    async def update_design(design_id: str, request: Request):
        body = await request.json()
        return svc.design_public(await run_in_threadpool(svc.update_design, design_id, body.get("values") or {}))

    @r.post("/api/designs/{design_id}/proof")
    def proof(design_id: str):
        return svc.proof(design_id)

    @r.post("/api/designs/{design_id}/approve")
    def approve(design_id: str):
        return svc.approve(design_id)

    @r.get("/api/designs/{design_id}/{filename}", include_in_schema=False)
    def design_file(design_id: str, filename: str):
        if not re.fullmatch(r"dsn_[A-Za-z0-9]+", design_id) or not re.fullmatch(
            r"(proof-p\d+\.jpg|proof\.pdf|thumbnail\.jpg)", filename
        ):
            raise NotFound("not found")
        path = svc.storage.path(f"designs/{design_id}/{filename}")
        if filename.endswith(".pdf"):
            return FileResponse(path, media_type="application/pdf", filename=f"{design_id}-proof.pdf")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    return r
