"""Backups: one zip per backup, holding every database row and every stored file.

    jasmin-backup-20261014T031500Z.zip
        manifest.json            what is inside, with a SHA-256 for every file
        database/<table>.json    the rows of each table
        files/<path>             every stored file, under its storage path

The zips are written to a folder outside the database and the file storage (a
folder on the app's own machine), so they survive the loss of either. A backup is
made from the admin, by ``python -m scripts.backup`` or once a day by the app, and
put back with ``python -m scripts.backup restore <zip>``.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import re
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .db import TABLES
from .storage import FOLDERS

log = logging.getLogger("jasmin")

FORMAT = 1
NAME = re.compile(r"jasmin-backup-(\d{8}T\d{6}Z)\.zip")
STAMP = "%Y%m%dT%H%M%SZ"
# An automatic backup is due when the newest one is older than this.
STALE = timedelta(hours=26)


class BackupError(RuntimeError):
    pass


class BackupBusy(BackupError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def next_run(now: datetime, hour: int, newest: datetime | None) -> datetime:
    """When the next automatic backup is due: soon when there is none or the newest is stale, else at ``hour`` UTC."""
    if newest is None or now - newest > STALE:
        return now + timedelta(minutes=2)
    at = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


class Backups:
    def __init__(self, svc, directory: Path, keep: int = 14):
        self.svc = svc
        self.dir = directory
        self.keep = max(1, keep)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.running = False
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._manifests: dict[tuple[str, int, int], dict] = {}

    # -- making one ----------------------------------------------------
    def create(self, kind: str = "manual") -> dict:
        """Write a new backup and remove the oldest beyond ``keep``. One at a time, across processes."""
        with (self.dir / ".lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackupBusy("A backup is already being made.") from exc
            created = datetime.now(timezone.utc).replace(microsecond=0)
            final = self.dir / f"jasmin-backup-{created.strftime(STAMP)}.zip"
            part = self.dir / f".{final.name}.part"
            try:
                manifest = self._write(part, created, kind)
                self._check(part, manifest)
                part.replace(final)
            finally:
                part.unlink(missing_ok=True)
            self.prune()
            return self._info(final)

    def _write(self, part: Path, created: datetime, kind: str) -> dict:
        db, storage = self.svc.db, self.svc.storage
        manifest: dict = {"format": FORMAT, "schema": db.version, "created": created.isoformat(), "kind": kind, "tables": {}, "files": {}, "bytes": 0}
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z, tempfile.TemporaryDirectory(dir=self.dir, prefix=".spool-") as spool:
            for table in TABLES:
                rows = db.all(f"SELECT * FROM {table}")
                manifest["tables"][table] = len(rows)
                z.writestr(f"database/{table}.json", json.dumps(rows, ensure_ascii=False))
            tmp = Path(spool) / "file"
            for key in storage.keys():
                if not storage.fetch(key, tmp):
                    continue  # removed while the backup was running
                manifest["files"][key] = _sha256(tmp)
                manifest["bytes"] += tmp.stat().st_size
                # PDFs and JPEGs are compressed already.
                z.write(tmp, f"files/{key}", compress_type=zipfile.ZIP_STORED)
                tmp.unlink()
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        return manifest

    @staticmethod
    def _check(path: Path, manifest: dict) -> None:
        """Read the finished zip back: it must be whole and hold what the manifest says."""
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                raise BackupError("the backup file is damaged")
            names = set(z.namelist())
            missing = [k for k in manifest["files"] if f"files/{k}" not in names] + [t for t in TABLES if f"database/{t}.json" not in names]
            if missing:
                raise BackupError(f"the backup is incomplete: {missing[:5]}")

    def start(self, kind: str = "manual") -> bool:
        """Make a backup on a thread of its own; False when one is already running."""
        if self.running:
            return False
        self.running, self.error = True, None

        def work() -> None:
            try:
                info = self.create(kind)
                log.info("backup %s made (%s files)", info["name"], info["files"])
            except BackupBusy:
                pass
            except Exception as exc:  # shown in the admin; the next one may well succeed
                log.exception("backup failed")
                self.error = str(exc) or exc.__class__.__name__
            finally:
                self.running = False

        self._thread = threading.Thread(target=work, name="jasmin-backup", daemon=True)
        self._thread.start()
        return True

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- the ones that exist -------------------------------------------
    def _files(self) -> list[Path]:
        return sorted((p for p in self.dir.iterdir() if NAME.fullmatch(p.name)), reverse=True)

    def _info(self, path: Path) -> dict:
        stat = path.stat()
        key = (path.name, stat.st_size, stat.st_mtime_ns)
        if key not in self._manifests:
            try:
                with zipfile.ZipFile(path) as z:
                    manifest = json.loads(z.read("manifest.json"))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                manifest = {}
            self._manifests = {k: v for k, v in self._manifests.items() if k[0] != path.name}
            self._manifests[key] = manifest
        m = self._manifests[key]
        created = m.get("created") or datetime.strptime(NAME.fullmatch(path.name)[1], STAMP).replace(tzinfo=timezone.utc).isoformat()
        return {
            "name": path.name,
            "created": created,
            "kind": m.get("kind", "manual"),
            "size": stat.st_size,
            "tables": m.get("tables", {}),
            "files": len(m.get("files", {})),
            "readable": bool(m),
        }

    def list(self) -> list[dict]:
        """Newest first."""
        return [self._info(p) for p in self._files()]

    def newest(self) -> datetime | None:
        files = self._files()
        return datetime.strptime(NAME.fullmatch(files[0].name)[1], STAMP).replace(tzinfo=timezone.utc) if files else None

    def path(self, name: str) -> Path | None:
        path = self.dir / name
        return path if NAME.fullmatch(name) and path.is_file() else None

    def prune(self) -> list[str]:
        gone = [p for p in self._files()[self.keep :]]
        for p in gone:
            p.unlink(missing_ok=True)
        return [p.name for p in gone]


class Scheduler(threading.Thread):
    """Makes one backup a day; the first one soon after start when the newest is stale or missing."""

    def __init__(self, backups: Backups, hour: int, before: Callable[[], object] | None = None):
        super().__init__(name="jasmin-backup-schedule", daemon=True)
        self.backups = backups
        self.hour = hour
        self.before = before  # the day's housekeeping, done just before the backup
        self._halt = threading.Event()
        self.due: datetime | None = None

    def run(self) -> None:
        while not self._halt.is_set():
            now = datetime.now(timezone.utc)
            self.due = next_run(now, self.hour, self.backups.newest())
            if self._halt.wait(max(1.0, (self.due - now).total_seconds())):
                return
            # One may have been made by hand while this waited: then the next is not due yet.
            again = datetime.now(timezone.utc)
            if next_run(again, self.hour, self.backups.newest()) > again + timedelta(minutes=5):
                continue
            if self.before is not None:
                try:
                    self.before()
                except Exception:  # the backup is made all the same
                    log.exception("daily housekeeping failed")
            if self.backups.start("automatic"):
                self.backups.wait()
            if self.backups.error or self.backups.newest() is None:
                self._halt.wait(3600)  # it failed: try again in an hour, not every two minutes

    def stop(self) -> None:
        self._halt.set()


def restore(zip_path: Path, db, storage, log: Callable[[str], None] = print, dry_run: bool = False) -> dict:
    """Put a backup into the configured database and storage.

    Rows that are already there are left as they are; files are written again over
    themselves. Nothing is removed, so restoring into a live site adds what is missing.
    """
    try:
        z = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupError(f"cannot read {zip_path}: {exc}") from exc
    with z:
        try:
            manifest = json.loads(z.read("manifest.json"))
        except (KeyError, ValueError) as exc:
            raise BackupError("this zip is not a Jasmin backup (no manifest)") from exc
        if manifest.get("format") != FORMAT:
            raise BackupError(f"unknown backup format {manifest.get('format')!r}")
        files = sorted(manifest["files"])
        bad = [k for k in files if k.split("/")[0] not in FOLDERS or ".." in k.split("/") or k.startswith("/")]
        if bad:
            raise BackupError(f"unexpected file paths in the backup: {bad[:3]}")
        names = set(z.namelist())
        # A table added after the backup was made is simply not in it.
        rows = {t: json.loads(z.read(f"database/{t}.json")) if f"database/{t}.json" in names else [] for t in TABLES}
        report: dict = {"created": manifest["created"], "files": {"backup": len(files)}, "tables": {}}
        log(f"backup of {manifest['created']}: " + ", ".join(f"{len(r)} {t}" for t, r in rows.items()) + f", {len(files)} files")
        if dry_run:
            log("dry run: nothing was restored")
            return report

        # Files first, so a restored row never points at a file that is not there yet.
        if storage.remote:
            storage.ensure_bucket()
        for n, key in enumerate(files, 1):
            dest = storage.root / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(f"files/{key}") as src, dest.open("wb") as out:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    out.write(chunk)
            if _sha256(dest) != manifest["files"][key]:
                raise BackupError(f"{key} in the backup does not match its checksum")
            storage.put(key)
            if n % 25 == 0 or n == len(files):
                log(f"  files {n}/{len(files)}")
        report["files"]["restored"] = len(files)

        for table, key in TABLES.items():
            before = db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            for row in rows[table]:
                cols = list(row)
                db.execute(
                    f"INSERT INTO {table} ({', '.join(f'\"{c}\"' for c in cols)}) VALUES ({', '.join('?' for _ in cols)})"
                    f" ON CONFLICT ({', '.join(key)}) DO NOTHING",
                    tuple(row[c] for c in cols),
                )
            added = db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"] - before
            report["tables"][table] = {"backup": len(rows[table]), "added": added, "already_there": len(rows[table]) - added}
            log(f"  {table}: {len(rows[table])} in the backup, {added} added, {len(rows[table]) - added} already there")
        db.reset_identity()  # restored order items kept their ids
        log("restore complete")
        return report
