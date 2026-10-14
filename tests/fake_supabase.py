"""An in-process stand-in for the Supabase Storage REST API, as an httpx transport.

It keeps objects in a dict and answers the calls app/storage.py makes: bucket
lookup and creation, upload, download, one-level listing and batch delete.
"""
from __future__ import annotations

import json
from urllib.parse import unquote

import httpx

SECRET = "test-secret-key"


class FakeSupabaseStorage:
    def __init__(self, buckets: tuple[str, ...] = ()):
        self.buckets: dict[str, dict] = {b: {"id": b, "name": b, "public": False} for b in buckets}
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}  # (bucket, key) -> (bytes, content type)
        self.requests: list[tuple[str, str]] = []
        self.fail_next: list[int] = []  # status codes to answer with before behaving again
        self.transport = httpx.MockTransport(self.handle)

    def keys(self, bucket: str = "jasmin") -> list[str]:
        return sorted(k for b, k in self.objects if b == bucket)

    @staticmethod
    def _error(status: int, code: str, message: str) -> httpx.Response:
        # The real service reports the HTTP-like code in the body as well.
        return httpx.Response(status, json={"statusCode": str(code), "error": message, "message": message})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        self.requests.append((request.method, path))
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), text="upstream trouble")
        if request.headers.get("authorization") != f"Bearer {SECRET}" or request.headers.get("apikey") != SECRET:
            return self._error(403, "403", "Invalid JWT")
        assert path.startswith("/storage/v1/"), path
        parts = path[len("/storage/v1/"):].split("/")

        if parts[0] == "bucket":
            if request.method == "GET" and len(parts) == 2:
                bucket = self.buckets.get(parts[1])
                return httpx.Response(200, json=bucket) if bucket else self._error(400, "404", "Bucket not found")
            if request.method == "POST" and len(parts) == 1:
                body = json.loads(request.content)
                if body["id"] in self.buckets:
                    return self._error(400, "409", "The resource already exists")
                self.buckets[body["id"]] = body
                return httpx.Response(200, json={"name": body["id"]})

        if parts[0] == "object" and len(parts) >= 2:
            if parts[1] == "list" and request.method == "POST":
                return self._list(parts[2], json.loads(request.content))
            bucket = parts[1]
            if bucket not in self.buckets:
                return self._error(400, "404", "Bucket not found")
            if request.method == "DELETE" and len(parts) == 2:
                gone = []
                for key in json.loads(request.content)["prefixes"]:
                    if self.objects.pop((bucket, key), None) is not None:
                        gone.append({"name": key})
                return httpx.Response(200, json=gone)
            key = "/".join(parts[2:])
            if request.method == "POST":
                if (bucket, key) in self.objects and request.headers.get("x-upsert") != "true":
                    return self._error(400, "409", "The resource already exists")
                self.objects[(bucket, key)] = (request.content, request.headers.get("content-type", ""))
                return httpx.Response(200, json={"Key": f"{bucket}/{key}", "Id": "fake"})
            if request.method == "GET":
                found = self.objects.get((bucket, key))
                if found is None:
                    return self._error(400, "404", "Object not found")
                return httpx.Response(200, content=found[0], headers={"content-type": found[1]})
        return self._error(404, "404", f"no such route: {request.method} {path}")

    def _list(self, bucket: str, body: dict) -> httpx.Response:
        if bucket not in self.buckets:
            return self._error(400, "404", "Bucket not found")
        prefix = body.get("prefix", "").strip("/")
        head = f"{prefix}/" if prefix else ""
        files, folders = set(), set()
        for b, key in self.objects:
            if b != bucket or not key.startswith(head):
                continue
            rest = key[len(head):]
            (folders if "/" in rest else files).add(rest.split("/")[0])
        entries = [{"name": n, "id": None} for n in sorted(folders)] + [{"name": n, "id": f"id-{n}"} for n in sorted(files)]
        entries.sort(key=lambda e: e["name"])
        offset, limit = body.get("offset", 0), body.get("limit", 100)
        return httpx.Response(200, json=entries[offset : offset + limit])


PUBLISHABLE = "test-publishable-key"


class FakeSupabaseAuth:
    """A stand-in for the Supabase Auth password sign-in (POST /auth/v1/token?grant_type=password)."""

    def __init__(self):
        self.users: dict[str, dict] = {}  # email -> {"password", "confirmed"}
        self.requests: list[httpx.Request] = []
        self.fail_with: int | Exception | None = None  # a status code to answer with, or an error to raise
        self.transport = httpx.MockTransport(self.handle)

    def add(self, email: str, password: str, confirmed: bool = True) -> None:
        self.users[email.lower()] = {"password": password, "confirmed": confirmed, "id": f"00000000-0000-4000-8000-{len(self.users) + 1:012d}"}

    def _admin(self, request: httpx.Request) -> httpx.Response:
        """The admin API (secret key): list, create, change and delete users."""
        if request.headers.get("apikey") != SECRET or request.headers.get("authorization") != f"Bearer {SECRET}":
            return httpx.Response(401, json={"message": "Invalid API key"})
        path = request.url.path.removeprefix("/auth/v1/admin/users").strip("/")
        public = lambda email, u: {"id": u["id"], "email": email, "email_confirmed_at": "2026-10-14T00:00:00Z" if u["confirmed"] else None}
        if request.method == "GET" and not path:
            page, per = int(request.url.params.get("page", 1)), int(request.url.params.get("per_page", 50))
            users = [public(e, u) for e, u in sorted(self.users.items())]
            return httpx.Response(200, json={"users": users[(page - 1) * per : page * per], "aud": "authenticated"})
        if request.method == "POST" and not path:
            body = json.loads(request.content)
            email = str(body["email"]).lower()
            if email in self.users:
                return httpx.Response(422, json={"code": 422, "error_code": "email_exists", "msg": "A user with this email address has already been registered"})
            if len(body.get("password", "")) < 6:
                return httpx.Response(422, json={"code": 422, "error_code": "weak_password", "msg": "Password should be at least 6 characters"})
            self.add(email, body["password"], bool(body.get("email_confirm")))
            return httpx.Response(200, json=public(email, self.users[email]))
        email = next((e for e, u in self.users.items() if u["id"] == path), None)
        if email is None:
            return httpx.Response(404, json={"code": 404, "error_code": "user_not_found", "msg": "User not found"})
        if request.method == "PUT":
            self.users[email]["password"] = json.loads(request.content)["password"]
            return httpx.Response(200, json=public(email, self.users[email]))
        if request.method == "DELETE":
            del self.users[email]
            return httpx.Response(200, json={})
        return httpx.Response(405)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.fail_with, Exception):
            raise self.fail_with
        if self.fail_with:
            return httpx.Response(self.fail_with, text="upstream trouble")
        if request.url.path.startswith("/auth/v1/admin/users"):
            return self._admin(request)
        assert request.method == "POST" and request.url.path == "/auth/v1/token", request.url
        assert request.url.params.get("grant_type") == "password"
        if request.headers.get("apikey") != PUBLISHABLE:
            return httpx.Response(401, json={"message": "Invalid API key"})
        body = json.loads(request.content)
        user = self.users.get(str(body.get("email", "")).lower())
        if user is None or user["password"] != body.get("password"):
            return httpx.Response(400, json={"code": 400, "error_code": "invalid_credentials", "msg": "Invalid login credentials"})
        stamp = "2026-10-14T00:00:00.000000Z" if user["confirmed"] else None
        return httpx.Response(
            200,
            json={
                "access_token": "fake-access-token",
                "token_type": "bearer",
                "expires_in": 3600,
                "expires_at": 1790000000,
                "refresh_token": "fake-refresh-token",
                "user": {
                    "id": user["id"],
                    "aud": "authenticated",
                    "role": "authenticated",
                    # Supabase stores and returns addresses in lower case.
                    "email": str(body["email"]).lower(),
                    "email_confirmed_at": stamp,
                    "confirmed_at": stamp,
                },
            },
        )
