"""Where the files in the data directory are kept for good.

The data directory is always the working copy: the PDF and image code needs
real file paths. With ``LocalStorage`` that is all there is. With
``SupabaseStorage`` every file is also stored in a private Supabase Storage
bucket under its path relative to the data directory (``pdfs/<file>``,
``templates/<id>/r<N>/...``, ``uploads/<file>``, ``designs/<id>/<file>``,
``print/<order>/<file>``, ``fonts/<file>``), and a file missing from the data
directory is fetched from the bucket when it is next needed. Nothing in the
bucket is public; the app keeps serving the files itself.
"""
from __future__ import annotations

import mimetypes
import os
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import Settings

LIST_PAGE = 100
DELETE_BATCH = 100
# The top-level folders of the data directory that hold stored files.
FOLDERS = ("pdfs", "templates", "uploads", "designs", "print", "fonts")


class StorageError(OSError):
    """The object store refused a request or could not be reached."""


def _clean(rel: str | os.PathLike) -> str | None:
    """``rel`` as a bucket key, or None if it could point outside the data directory."""
    parts = str(rel).replace("\\", "/").strip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return "/".join(parts)


class LocalStorage:
    """Files live in the data directory only."""

    remote = False

    def __init__(self, root: Path):
        self.root = root

    def put(self, rel: str) -> None:
        pass

    def put_tree(self, rel_dir: str) -> None:
        pass

    def ensure(self, rel: str) -> bool:
        return (self.root / rel).is_file()

    def ensure_tree(self, rel_dir: str) -> bool:
        return (self.root / rel_dir).exists()

    def sync_down(self, rel_dir: str) -> None:
        pass

    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()

    def delete_tree(self, rel_dir: str) -> None:
        pass

    def delete(self, rels: list[str]) -> None:
        """Remove single files for good (the copies in the data directory are the caller's)."""

    def keys(self) -> list[str]:
        """Every stored file, as a path relative to the data directory."""
        return sorted(p.relative_to(self.root).as_posix() for f in FOLDERS for p in (self.root / f).rglob("*") if p.is_file())

    def fetch(self, rel: str, dest: Path) -> bool:
        """Copy one stored file to ``dest`` (anywhere); False when it does not exist."""
        src = self.root / rel
        if not src.is_file():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return True

    def close(self) -> None:
        pass


class SupabaseStorage:
    """The data directory, written through to a private Supabase Storage bucket."""

    remote = True

    def __init__(
        self,
        root: Path,
        url: str,
        secret_key: str,
        bucket: str = "jasmin",
        transport: httpx.BaseTransport | None = None,
    ):
        self.root = root
        self.bucket = bucket
        self._http = httpx.Client(
            base_url=url.rstrip("/") + "/storage/v1",
            headers={"Authorization": f"Bearer {secret_key}", "apikey": secret_key},
            timeout=httpx.Timeout(120, connect=10),
            transport=transport,
        )

    # -- HTTP ----------------------------------------------------------
    def _request(self, method: str, path: str, *, ok: tuple[int, ...] = (200,), **kwargs) -> httpx.Response:
        """One request, retried once on a connection error or a 5xx."""
        for attempt in (1, 2):
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == 1:
                    time.sleep(0.5)
                    continue
                raise StorageError(f"storage unreachable: {exc}") from exc
            if resp.status_code >= 500 and attempt == 1:
                time.sleep(0.5)
                continue
            if resp.status_code not in ok:
                raise StorageError(f"storage {method} {path}: HTTP {resp.status_code} {resp.text[:200]}")
            return resp
        raise AssertionError("unreachable")

    def _object(self, key: str) -> str:
        return f"/object/{quote(self.bucket, safe='')}/{quote(key, safe='/')}"

    @staticmethod
    def _missing(resp: httpx.Response) -> bool:
        """Storage answers "no such object" with 404, or with 400 and a body that says 404 / not found."""
        if resp.status_code == 404:
            return True
        if resp.status_code != 400:
            return False
        try:
            body = resp.json()
        except ValueError:
            return False
        said = f"{body.get('statusCode', '')} {body.get('error', '')} {body.get('message', '')}".lower()
        return "404" in said or "not found" in said or "not_found" in said

    def ensure_bucket(self) -> None:
        """Create the private bucket if it is not there yet."""
        resp = self._request("GET", f"/bucket/{quote(self.bucket, safe='')}", ok=(200, 400, 404))
        if resp.status_code == 200:
            return
        if not self._missing(resp):
            raise StorageError(f"storage bucket {self.bucket!r}: HTTP {resp.status_code} {resp.text[:200]}")
        resp = self._request("POST", "/bucket", ok=(200, 201, 400, 409), json={"id": self.bucket, "name": self.bucket, "public": False})
        if resp.status_code in (400, 409) and "exist" not in resp.text.lower() and "duplicate" not in resp.text.lower():
            raise StorageError(f"storage could not create bucket {self.bucket!r}: HTTP {resp.status_code} {resp.text[:200]}")

    # -- listing -------------------------------------------------------
    def _list(self, prefix: str) -> tuple[list[str], list[str]]:
        """The files and the folders directly under ``prefix`` (names only)."""
        files: list[str] = []
        folders: list[str] = []
        offset = 0
        while True:
            body = {"prefix": prefix, "limit": LIST_PAGE, "offset": offset, "sortBy": {"column": "name", "order": "asc"}}
            entries = self._request("POST", f"/object/list/{quote(self.bucket, safe='')}", json=body).json()
            for entry in entries:
                (folders if entry.get("id") is None else files).append(entry["name"])
            if len(entries) < LIST_PAGE:
                return files, folders
            offset += len(entries)

    def _walk(self, prefix: str) -> list[str]:
        """Every object key under ``prefix``, however deep."""
        files, folders = self._list(prefix)
        keys = [f"{prefix}/{name}" for name in files]
        for name in folders:
            keys.extend(self._walk(f"{prefix}/{name}"))
        return keys

    # -- the interface -------------------------------------------------
    def put(self, rel: str) -> None:
        """Upload one file from the data directory, replacing what is there."""
        key = _clean(rel)
        if key is None:
            raise StorageError(f"not a storable path: {rel!r}")
        path = self.root / key
        headers = {"Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream", "x-upsert": "true"}
        self._request("POST", self._object(key), content=path.read_bytes(), headers=headers)

    def put_tree(self, rel_dir: str) -> None:
        """Upload every file under a directory of the data directory."""
        base = self.root / rel_dir
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            self.put(path.relative_to(self.root).as_posix())

    def _download(self, key: str, dest: Path) -> bool:
        resp = self._request("GET", self._object(key), ok=(200, 400, 404))
        if resp.status_code != 200:
            if self._missing(resp):
                return False
            raise StorageError(f"storage GET {key}: HTTP {resp.status_code} {resp.text[:200]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target, then renamed: a crash never leaves half a file.
        tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.part")
        try:
            tmp.write_bytes(resp.content)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
        return True

    def ensure(self, rel: str) -> bool:
        """Make sure the file is in the data directory; False if it exists nowhere."""
        key = _clean(rel)
        if key is None:
            return False
        path = self.root / key
        return path.is_file() or self._download(key, path)

    def ensure_tree(self, rel_dir: str) -> bool:
        """Make sure the whole directory is in the data directory; False if it exists nowhere.

        A directory that is already there may hold only the files fetched one by
        one so far, so the bucket's listing decides what is still missing.
        """
        key = _clean(rel_dir)
        if key is None:
            return False
        keys = self._walk(key)
        for k in keys:
            if not (self.root / k).is_file() and not self._download(k, self.root / k):
                raise StorageError(f"storage lost {k} while it was being fetched")
        return bool(keys) or (self.root / key).exists()

    def sync_down(self, rel_dir: str) -> None:
        """Fetch the files under a directory that the data directory does not have yet."""
        key = _clean(rel_dir)
        if key is None:
            return
        for k in self._walk(key):
            if not (self.root / k).is_file():
                self._download(k, self.root / k)

    def exists(self, rel: str) -> bool:
        key = _clean(rel)
        if key is None:
            return False
        if (self.root / key).is_file():
            return True
        parent, _, name = key.rpartition("/")
        return name in self._list(parent)[0]

    def delete_tree(self, rel_dir: str) -> None:
        """Remove every object under a directory from the bucket (the data directory is the caller's)."""
        key = _clean(rel_dir)
        if key is None:
            return
        self.delete(self._walk(key))

    def delete(self, rels: list[str]) -> None:
        """Remove single objects from the bucket; ones that are not there are no error."""
        keys = [k for k in (_clean(r) for r in rels) if k]
        for i in range(0, len(keys), DELETE_BATCH):
            self._request("DELETE", f"/object/{quote(self.bucket, safe='')}", json={"prefixes": keys[i : i + DELETE_BATCH]})

    def keys(self) -> list[str]:
        """Every object in the bucket, as a path relative to the data directory."""
        return sorted(key for f in FOLDERS for key in self._walk(f))

    def fetch(self, rel: str, dest: Path) -> bool:
        """Download one object to ``dest`` (anywhere); False when it does not exist."""
        return self._download(rel, dest)

    def close(self) -> None:
        self._http.close()


Storage = LocalStorage | SupabaseStorage


def open_storage(settings: Settings) -> Storage:
    """Supabase Storage when its URL and secret key are set, otherwise the data directory alone."""
    if settings.supabase_url and settings.supabase_secret_key:
        return SupabaseStorage(settings.data_dir, settings.supabase_url, settings.supabase_secret_key, settings.storage_bucket)
    return LocalStorage(settings.data_dir)
