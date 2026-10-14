"""Admin sign-in and admin accounts with Supabase Auth.

The password is checked by Supabase (server to server, no JavaScript SDK). A
successful sign-in by an admin gets a session cookie signed by this app;
Supabase's own tokens are not kept.

Who is an admin: the addresses on the ADMIN_EMAILS list (the owners, set where
the app is installed) and the addresses in the ``admins`` table, which admins
add and remove themselves on the Account page. An admin changes their own
password there too. Accounts are made, changed and removed in Supabase with the
secret key, which never leaves the server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
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


class AccountError(ValueError):
    """Something about an account request that the person can put right; the message is shown to them."""


MIN_PASSWORD = 10
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
ALLOW_CACHE_SECONDS = 20


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class AdminAuth:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None, db=None):
        self.settings = settings
        self.db = db
        self._added: tuple[float, frozenset[str]] = (0.0, frozenset())
        self._admin_http: httpx.Client | None = None
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
        if settings.supabase_secret_key:
            self._admin_http = httpx.Client(
                base_url=settings.supabase_url.rstrip("/") + "/auth/v1/admin",
                headers={"apikey": settings.supabase_secret_key, "Authorization": f"Bearer {settings.supabase_secret_key}"},
                timeout=httpx.Timeout(15, connect=10),
                transport=transport,
            )

    # -- Who is an admin -------------------------------------------------
    def _added_admins(self) -> frozenset[str]:
        """The addresses in the admins table. Read at most every few seconds: this is asked on every admin request."""
        at, emails = self._added
        if self.db is None:
            return emails
        if not at or time.monotonic() - at > ALLOW_CACHE_SECONDS:
            emails = frozenset(r["email"] for r in self.db.all("SELECT email FROM admins"))
            self._added = (time.monotonic(), emails)
        return emails

    def allowed(self, email: str) -> bool:
        email = email.strip().lower()
        return email in self.settings.admin_emails or email in self._added_admins()

    # -- Sign-in ---------------------------------------------------------
    def _grant(self, email: str, password: str) -> dict:
        """Supabase's user for a correct address and password; raises SignInRefused or SignInUnavailable."""
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
            return resp.json().get("user") or {}
        except ValueError as exc:
            raise SignInUnavailable("unexpected answer") from exc

    def sign_in(self, email: str, password: str) -> str:
        """The signed-in address, lower-cased; raises SignInRefused or SignInUnavailable."""
        user = self._grant(email, password)
        address = str(user.get("email") or "").strip().lower()
        if not address or not (user.get("email_confirmed_at") or user.get("confirmed_at")):
            raise SignInRefused("address not confirmed")
        if not self.allowed(address):
            log.warning("admin sign-in refused: %s is not an admin", address)
            raise SignInRefused("address not on the list")
        return address

    # -- Accounts --------------------------------------------------------
    def _admin(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self._admin_http is None:
            raise SignInUnavailable("SUPABASE_SECRET_KEY is not set")
        try:
            return self._admin_http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SignInUnavailable(str(exc)) from exc

    def _find_user(self, email: str) -> dict | None:
        page = 1
        while True:
            resp = self._admin("GET", "/users", params={"page": page, "per_page": 200})
            if resp.status_code != 200:
                raise SignInUnavailable(f"HTTP {resp.status_code}")
            users = resp.json().get("users") or []
            for user in users:
                if str(user.get("email") or "").lower() == email:
                    return user
            if len(users) < 200:
                return None
            page += 1

    @staticmethod
    def _check_password(password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise AccountError(f"The password must be at least {MIN_PASSWORD} characters long.")
        if len(password) > 200:
            raise AccountError("The password is too long.")

    def _set_password(self, user_id: str, password: str) -> None:
        resp = self._admin("PUT", f"/users/{user_id}", json={"password": password})
        if resp.status_code == 422:
            raise AccountError("That password was not accepted. Choose a longer or less common one.")
        if resp.status_code != 200:
            raise SignInUnavailable(f"HTTP {resp.status_code}")

    def change_password(self, email: str, current: str, new: str) -> None:
        """Change the signed-in admin's own password; the current one must be right."""
        self._check_password(new)
        if new == current:
            raise AccountError("The new password is the same as the current one.")
        try:
            user = self._grant(email, current)
        except SignInRefused as exc:
            raise AccountError("The current password is not right.") from exc
        self._set_password(str(user["id"]), new)
        log.info("admin %s changed their password", email)

    def admins(self) -> list[dict]:
        """Owners first, then the admins that were added, oldest first."""
        owners = [{"email": e, "owner": True, "addedBy": "", "createdAt": ""} for e in sorted(self.settings.admin_emails)]
        rows = self.db.all("SELECT * FROM admins ORDER BY created_at, email") if self.db is not None else []
        return owners + [
            {"email": r["email"], "owner": False, "addedBy": r["added_by"], "createdAt": r["created_at"]}
            for r in rows
            if r["email"] not in self.settings.admin_emails
        ]

    def add_admin(self, email: str, password: str, added_by: str) -> None:
        """Give a new person an account with this password and let them into the admin."""
        from .db import now

        email = email.strip().lower()
        if not EMAIL.fullmatch(email) or len(email) > 200:
            raise AccountError("That does not look like an email address.")
        if email in self.settings.admin_emails or self.db.one("SELECT email FROM admins WHERE email=?", (email,)):
            raise AccountError(f"{email} is already an admin.")
        self._check_password(password)
        resp = self._admin("POST", "/users", json={"email": email, "password": password, "email_confirm": True})
        if resp.status_code in (400, 409, 422):
            # The address has an account already (made in Supabase itself): it gets this password.
            user = self._find_user(email)
            if user is None:
                raise AccountError("That address or password was not accepted.")
            self._set_password(str(user["id"]), password)
        elif resp.status_code not in (200, 201):
            raise SignInUnavailable(f"HTTP {resp.status_code}")
        self.db.execute("INSERT INTO admins (email, added_by, created_at) VALUES (?,?,?) ON CONFLICT (email) DO NOTHING", (email, added_by, now()))
        self._added = (0.0, frozenset())
        log.info("admin %s added by %s", email, added_by)

    def remove_admin(self, email: str, removed_by: str) -> None:
        """Take an added admin's access away and delete their account. Owners and oneself cannot be removed."""
        email = email.strip().lower()
        if email == removed_by:
            raise AccountError("You cannot remove yourself.")
        if email in self.settings.admin_emails:
            raise AccountError(f"{email} is an owner and can only be removed where the app is installed.")
        if self.db.one("SELECT email FROM admins WHERE email=?", (email,)) is None:
            raise AccountError(f"{email} is not an admin.")
        # Access first: whatever happens next, they are out.
        self.db.execute("DELETE FROM admins WHERE email=?", (email,))
        self._added = (0.0, frozenset())
        log.info("admin %s removed by %s", email, removed_by)
        user = self._find_user(email)
        if user is not None:
            resp = self._admin("DELETE", f"/users/{user['id']}")
            if resp.status_code not in (200, 204, 404):
                raise SignInUnavailable(f"HTTP {resp.status_code}")

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
        if self._admin_http is not None:
            self._admin_http.close()
