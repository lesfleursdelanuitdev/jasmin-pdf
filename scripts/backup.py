"""Make, list or restore backups (see app/backup.py).

    python -m scripts.backup                       # make one now
    python -m scripts.backup list
    python -m scripts.backup restore <zip>         # put a backup into the configured database and storage
    python -m scripts.backup restore <zip> --dry-run

Restoring adds what is missing and removes nothing: rows that are already there
are left as they are.
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.backup import BackupBusy, BackupError, Backups, restore
from app.config import load_settings
from app.services import Services


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    settings = load_settings()
    svc = Services(settings)
    backups = Backups(svc, settings.backup_dir or settings.data_dir / "backups", settings.backup_keep)
    try:
        if not args:
            info = backups.create("manual")
            print(f"{info['name']}: {info['files']} files, " + ", ".join(f"{n} {t}" for t, n in info["tables"].items()) + f", {info['size']} bytes")
        elif args == ["list"]:
            for b in backups.list():
                print(f"{b['name']}  {b['kind']:9}  {b['size']:>12} bytes  {b['files']} files")
        elif len(args) == 2 and args[0] == "restore":
            restore(Path(args[1]), svc.db, svc.storage, dry_run="--dry-run" in argv)
        else:
            print(__doc__)
            return 2
    except BackupBusy as exc:
        print(str(exc))
        return 1
    except BackupError as exc:
        print(f"FAILED: {exc}")
        return 1
    finally:
        svc.db.close()
        svc.storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
