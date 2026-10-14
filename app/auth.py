"""Admin sign-in with Supabase Auth.

The password is checked by Supabase (server to server, no JavaScript SDK). A
successful sign-in by an address on the ADMIN_EMAILS list gets a session cookie
signed by this app; Supabase's own tokens are not kept.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlsplit

import httpx

from .config import Settings

log = logging.getLogger("jasmin")

COOKIE = "jasmin_admin"
SESSION_SECONDS = 12 * 3600
MAX_ATTEMPTS = 10
ATTEMPT_WINDOW = 5 * 60


class SignInRefused(Exception):
    """Wrong password, unknown or unconfirmed user, or an address that is not on the list."""


class SignInUnavailable(Exception):
    """Supabase could not be reached or did not answer as expected."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class AdminAuth:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.enabled = bool(settings.supabase_url and settings.supabase_publishable_key and settings.admin_emails)
        self._secret = settings.session_secret.encode()
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._http: httpx.Client | None = None
        if not self.enabled:
            return
        if not self._secret:
            self._secret = secrets.token_bytes(32)
            log.warning("SESSION_SECRET is not set: admin sign-ins will not survive a restart")
        self._http = httpx.Client(
            base_url=settings.supabase_url.rstrip("/") + "/auth/v1",
            headers={"apikey": settings.supabase_publishable_key},
            timeout=httpx.Timeout(15, connect=10),
            transport=transport,
        )

    # -- Sign-in ---------------------------------------------------------
    def allowed(self, email: str) -> bool:
        return email.strip().lower() in self.settings.admin_emails

    def sign_in(self, email: str, password: str) -> str:
        """The signed-in address, lower-cased; raises SignInRefused or SignInUnavailable."""
        assert self._http is not None
        try:
            resp = self._http.post("/token", params={"grant_type": "password"}, json={"email": email, "password": password})
        except httpx.HTTPError as exc:
            raise SignInUnavailable(str(exc)) from exc
        if resp.status_code in (400, 401, 403, 422):
            raise SignInRefused("refused by Supabase")
        if resp.status_code != 200:
            raise SignInUnavailable(f"HTTP {resp.status_code}")
        try:
            user = resp.json().get("user") or {}
        except ValueError as exc:
            raise SignInUnavailable("unexpected answer") from exc
        address = str(user.get("email") or "").strip().lower()
        if not address or not (user.get("email_confirmed_at") or user.get("confirmed_at")):
            raise SignInRefused("address not confirmed")
        if not self.allowed(address):
            log.warning("admin sign-in refused: %s is not on ADMIN_EMAILS", address)
            raise SignInRefused("address not on the list")
        return address

    # -- Session cookie --------------------------------------------------
    def _sign(self, body: str) -> str:
        return hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()

    def issue(self, email: str, now: float | None = None) -> str:
        expires = int((time.time() if now is None else now) + SESSION_SECONDS)
        body = _b64(json.dumps({"e": email, "x": expires}, separators=(",", ":")).encode())
        return f"{body}.{self._sign(body)}"

    def read(self, cookie: str | None) -> str | None:
        """The address a session cookie belongs to, or None if it is missing, tampered with, expired or no longer allowed."""
        if not self.enabled or not cookie or "." not in cookie:
            return None
        body, _, signature = cookie.rpartition(".")
        if not hmac.compare_digest(self._sign(body), signature):
            return None
        try:
            data = json.loads(_unb64(body))
            email, expires = str(data["e"]), float(data["x"])
        except (ValueError, KeyError, TypeError):
            return None
        if expires < time.time() or not self.allowed(email):
            return None
        return email

    @property
    def secure_cookie(self) -> bool:
        return self.settings.app_url.startswith("https")

    # -- Requests --------------------------------------------------------
    def same_origin(self, origin: str | None, host: str | None) -> bool:
        """Whether a request's Origin header (if any) is this app's own."""
        if not origin:
            return True
        app = urlsplit(self.settings.app_url)
        sent = urlsplit(origin)
        return (sent.scheme, sent.netloc) == (app.scheme, app.netloc) or (bool(host) and sent.netloc == host)

    def throttled(self, client: str) -> bool:
        """Records a sign-in attempt; True once a client has made too many in the window."""
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts.setdefault(client, deque())
            while attempts and attempts[0] <= now - ATTEMPT_WINDOW:
                attempts.popleft()
            if len(attempts) >= MAX_ATTEMPTS:
                return True
            attempts.append(now)
            for key in [k for k, v in self._attempts.items() if not v]:
                del self._attempts[key]
            return False

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
