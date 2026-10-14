"""The pages: the editor, the admin, and admin sign-in."""
from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..auth import COOKIE, SESSION_SECONDS, SignInRefused, SignInUnavailable
from . import NO_FRAMING, STATIC, Studio

log = logging.getLogger("jasmin")


def router(studio: Studio) -> APIRouter:
    r = APIRouter(include_in_schema=False)
    settings, auth = studio.settings, studio.auth

    @r.get("/")
    def index():
        return RedirectResponse("/demo-store" if settings.demo_store else "/admin")

    @r.get("/favicon.ico")
    def favicon():
        return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")

    @r.get("/editor/{template_id}")
    def editor_page(template_id: str):
        # The one page that a storefront may show inside itself.
        return FileResponse(STATIC / "editor.html", headers={"Content-Security-Policy": "frame-ancestors " + " ".join(settings.frame_ancestors)})

    if not auth.enabled:

        @r.get("/admin", dependencies=[Depends(studio.require_admin)])
        def admin_page_basic():
            return FileResponse(STATIC / "admin.html", headers=NO_FRAMING)

        @r.get("/admin/login")
        def no_login_form():
            return RedirectResponse("/admin", status_code=303)

        return r

    @r.get("/admin")
    def admin_page(request: Request):
        if studio.signed_in_as(request) is None:
            return RedirectResponse("/admin/login", status_code=303)
        return FileResponse(STATIC / "admin.html", headers={"Cache-Control": "no-store", **NO_FRAMING})

    def login_page(message: str = "", email: str = "", status: int = 200) -> HTMLResponse:
        page = (STATIC / "login.html").read_text(encoding="utf-8")
        notice = f'<p class="notice notice-bad" role="alert">{html.escape(message)}</p>' if message else ""
        page = page.replace("<!--message-->", notice).replace("{{email}}", html.escape(email, quote=True))
        return HTMLResponse(page, status_code=status, headers={"Cache-Control": "no-store", **NO_FRAMING})

    @r.get("/admin/login")
    def login_form(request: Request):
        if studio.signed_in_as(request) is not None:
            return RedirectResponse("/admin", status_code=303)
        return login_page()

    @r.post("/admin/login", dependencies=[Depends(studio.own_origin)])
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

    @r.post("/admin/logout", dependencies=[Depends(studio.own_origin)])
    def logout():
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(COOKIE, path="/admin", httponly=True, samesite="lax", secure=auth.secure_cookie)
        return response

    return r
