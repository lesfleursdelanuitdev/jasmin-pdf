"""Copy an existing data directory (SQLite file and files) into the configured database and storage.

    python -m scripts.copy_data /path/to/old/data            # copy
    python -m scripts.copy_data /path/to/old/data --dry-run  # only say what would be copied

The destination is whatever the environment configures (DATABASE_URL, SUPABASE_URL, ...).
The source is only read: the SQLite file is opened from a temporary copy, and nothing in
the source directory is changed or removed. Running it again is safe: rows that are
already there are left as they are, and files are uploaded again over themselves.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Callable

from app.config import load_settings
from app.db import PostgresDatabase, open_database
from app.storage import SupabaseStorage

# Parents before children; each with the column(s) that identify a row.
TABLES = {
    "templates": ("id",),
    "uploads": ("id",),
    "designs": ("id",),
    "orders": ("id",),
    "order_items": ("order_id", "line_item_id"),
}
FOLDERS = ("pdfs", "templates", "uploads", "designs", "print", "fonts")


class CopyError(RuntimeError):
    pass


def read_source(source: Path) -> dict[str, list[dict]]:
    """Every row of the source database, read from a temporary copy of its files."""
    db_file = source / "jasmin.db"
    if not db_file.is_file():
        raise CopyError(f"no jasmin.db in {source}")
    with tempfile.TemporaryDirectory() as tmp:
        for path in source.glob("jasmin.db*"):  # the database and its write-ahead log
            shutil.copy2(path, Path(tmp) / path.name)
        conn = sqlite3.connect(str(Path(tmp) / "jasmin.db"))
        conn.row_factory = sqlite3.Row
        try:
            return {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in TABLES}
        finally:
            conn.close()


def source_files(source: Path) -> list[str]:
    return sorted(
        p.relative_to(source).as_posix() for folder in FOLDERS for p in (source / folder).rglob("*") if p.is_file()
    )


def copy_data(source: Path, db, storage, log: Callable[[str], None] = print, dry_run: bool = False) -> dict:
    """Copy files, then rows. Returns what was found, copied and left alone, per table and for files."""
    rows = read_source(source)
    files = source_files(source)
    report: dict = {"files": {"source": len(files)}, "tables": {}}
    log(f"source {source}: " + ", ".join(f"{len(r)} {t}" for t, r in rows.items()) + f", {len(files)} files")
    if dry_run:
        log("dry run: nothing was copied")
        return report

    # Files first, so a copied row never points at a file that is not there yet.
    storage.ensure_bucket()
    for n, rel in enumerate(files, 1):
        storage.put(rel)
        if n % 25 == 0 or n == len(files):
            log(f"  files {n}/{len(files)}")
    stored = {key for folder in FOLDERS for key in storage._walk(folder)}
    missing = [rel for rel in files if rel not in stored]
    report["files"].update(stored=len(files) - len(missing), missing=missing)

    for table, key in TABLES.items():
        before = db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        for row in rows[table]:
            cols = list(row)
            db.execute(
                f"INSERT INTO {table} ({', '.join(f'\"{c}\"' for c in cols)}) VALUES ({', '.join('?' for _ in cols)})"
                f" ON CONFLICT ({', '.join(key)}) DO NOTHING",
                tuple(row[c] for c in cols),
            )
        after = db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        report["tables"][table] = {"source": len(rows[table]), "added": after - before, "already_there": len(rows[table]) - (after - before), "total": after}
        log(f"  {table}: {len(rows[table])} in the source, {after - before} added, {len(rows[table]) - (after - before)} already there")
    if isinstance(db, PostgresDatabase):
        # Copied order items kept their ids; new ones must be numbered after them.
        db.execute("SELECT setval(pg_get_serial_sequence('order_items', 'id'), GREATEST((SELECT MAX(id) FROM order_items), 1))")

    # Every source row must now be in the destination.
    absent = []
    for table, key in TABLES.items():
        where = " AND ".join(f"{k}=?" for k in key)
        absent += [f"{table} {[row[k] for k in key]}" for row in rows[table] if db.one(f"SELECT 1 AS x FROM {table} WHERE {where}", tuple(row[k] for k in key)) is None]
    report["absent"] = absent
    if missing or absent:
        raise CopyError(f"copy incomplete: {len(missing)} file(s) and {len(absent)} row(s) did not arrive: {(missing + absent)[:5]}")
    log("copy complete: every source row and file is in the destination; the source was not changed")
    return report


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__)
        return 2
    settings = load_settings()
    if not (settings.database_url and settings.supabase_url and settings.supabase_secret_key):
        print("DATABASE_URL, SUPABASE_URL and SUPABASE_SECRET_KEY must be set: there is no destination to copy to.")
        return 2
    source = Path(args[0]).resolve()
    db = open_database(settings.data_dir / "unused.db", settings.database_url, settings.database_url_unpooled)
    storage = SupabaseStorage(source, settings.supabase_url, settings.supabase_secret_key, settings.storage_bucket)
    try:
        copy_data(source, db, storage, dry_run="--dry-run" in argv)
    except CopyError as exc:
        print(f"FAILED: {exc}")
        return 1
    finally:
        db.close()
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
