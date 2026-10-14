"""Remove photos and drafts that customers left behind and never ordered (see Services.cleanup).

    python -m scripts.cleanup            # with RETENTION_DAYS from the environment
    python -m scripts.cleanup 20         # older than 20 days
    python -m scripts.cleanup --dry-run  # only count them
"""
from __future__ import annotations

import sys

from app.config import load_settings
from app.services import Services


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    settings = load_settings()
    days = int(args[0]) if args else settings.retention_days
    if days <= 0:
        print("No number of days given and RETENTION_DAYS is not set: nothing to do.")
        return 2
    svc = Services(settings)
    try:
        dry = "--dry-run" in argv
        report = svc.cleanup(days, dry_run=dry)
        print(f"{'would remove' if dry else 'removed'} {report['drafts']} drafts and {report['uploads']} photos older than {days} days")
    finally:
        svc.db.close()
        svc.storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
