"""The HTTP routes, one module per audience. Each has ``router(studio)``; ``app.main`` puts them together.

    pages     the pages themselves, and admin sign-in
    public    what the customer editor and the storefront call (/api, /fonts, /assets/uploads)
    webhooks  what Shopify calls
    admin     /admin/api
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ..auth import COOKIE, AdminAuth
from ..backup import Backups, Scheduler
from ..config import ROOT, Settings
from ..jobs import Jobs
from ..services import Services
from ..shopify import ShopifyAdmin

STATIC = ROOT / "static"
# The admin and its sign-in page are never shown inside another page (the customer editor is).
NO_FRAMING = {"Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"}


@dataclass
class Studio:
    """Everything a route may need, made once in ``create_app``."""

    settings: Settings
    svc: Services
    auth: AdminAuth
    shop_admin: ShopifyAdmin
    backups: Backups
    schedule: Scheduler | None
    jobs: Jobs

    def own_origin(self, request: Request) -> None:
        # A browser sends Origin with every cross-site POST, PATCH, PUT and DELETE.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not self.auth.same_origin(
            request.headers.get("origin"), request.headers.get("host")
        ):
            raise HTTPException(403, "Request from another site refused")

    def signed_in_as(self, request: Request) -> str | None:
        return self.auth.read(request.cookies.get(COOKIE))

    @property
    def require_admin(self):
        """The dependency that lets only an admin through: a session with Supabase sign-in, HTTP Basic without."""
        if self.auth.enabled:

            def require_session(request: Request) -> None:
                self.own_origin(request)
                if self.signed_in_as(request) is None:
                    raise HTTPException(401, "Sign in required")

            return require_session

        basic = HTTPBasic(realm="Jasmin admin")
        settings = self.settings

        def require_basic(creds: HTTPBasicCredentials = Depends(basic)) -> None:
            ok_user = secrets.compare_digest(creds.username.encode(), settings.admin_user.encode())
            ok_pass = secrets.compare_digest(creds.password.encode(), settings.admin_password.encode())
            if not (ok_user and ok_pass):
                raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": 'Basic realm="Jasmin admin"'})

        return require_basic
