"""What Shopify calls."""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import shopify
from . import Studio


def router(studio: Studio) -> APIRouter:
    r = APIRouter()
    settings, svc, jobs = studio.settings, studio.svc, studio.jobs

    @r.post("/webhooks/shopify/orders-create")
    async def orders_create(request: Request):
        body = await request.body()
        if not shopify.verify_webhook(body, request.headers.get("X-Shopify-Hmac-Sha256"), settings.webhook_secrets):
            return JSONResponse({"detail": "invalid signature"}, status_code=401)
        shop = request.headers.get("X-Shopify-Shop-Domain", "")
        if settings.shopify_shop_domain and shop and shop != settings.shopify_shop_domain:
            return JSONResponse({"detail": "unexpected shop"}, status_code=403)
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"detail": "invalid JSON"}, status_code=400)
        source = "demo" if request.headers.get("X-Jasmin-Demo") == "1" else "shopify"
        order = await run_in_threadpool(svc.record_order, payload, source=source, shop=shop)
        if order is not None:
            # Shopify only needs a fast 2xx; the print files are a job (it survives a restart).
            await run_in_threadpool(jobs.enqueue, "print", f"print:{order['id']}", {"order_id": order["id"]})
        return {"ok": True, "personalized": order is not None}

    return r
