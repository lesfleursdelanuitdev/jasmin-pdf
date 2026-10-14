"""Backups: everything goes into one zip, comes back out of it, and the admin can make and download one."""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.backup import BackupBusy, BackupError, Backups, next_run, restore
from app.config import Settings
from app.main import create_app
from app.shopify import sign
from app.storage import SupabaseStorage
from scripts.seed import seed

from .fake_supabase import SECRET, FakeSupabaseStorage
from .test_api import approved_design, order_payload

URL = "https://proj.supabase.co"


def fill(app) -> dict:
    """A template, a customer photo, an approved design and an order with its print file."""
    client = TestClient(app)
    seed(app.state.services)
    design = approved_design(client)
    body = json.dumps(order_payload(design["id"])).encode()
    r = client.post("/webhooks/shopify/orders-create", content=body, headers={"X-Shopify-Hmac-Sha256": sign(body, "s3cret"), "Content-Type": "application/json"})
    assert r.status_code == 200
    return design


@pytest.fixture(params=["local", "supabase"])
def site(request, settings):
    """The app with its files on disk only, and with its files in (fake) Supabase Storage."""
    fake = FakeSupabaseStorage()
    storage = SupabaseStorage(settings.data_dir, URL, SECRET, "jasmin", transport=fake.transport) if request.param == "supabase" else None
    app = create_app(settings, storage=storage)
    design = fill(app)
    yield app, design
    app.state.services.db.close()


def test_a_backup_holds_every_row_and_every_file(site):
    app, design = site
    svc, backups = app.state.services, app.state.backups
    info = backups.create()
    assert info["kind"] == "manual" and info["readable"]
    assert info["tables"] == {"templates": 1, "uploads": 1, "designs": 1, "orders": 1, "order_items": 1, "admins": 0}
    assert info["files"] == len(svc.storage.keys()) > 10
    with zipfile.ZipFile(backups.path(info["name"])) as z:
        assert z.testzip() is None
        manifest = json.loads(z.read("manifest.json"))
        assert set(manifest["files"]) == set(svc.storage.keys())
        assert json.loads(z.read("database/designs.json"))[0]["id"] == design["id"]
        pdf = next(k for k in manifest["files"] if k.startswith("print/"))
        assert z.read(f"files/{pdf}").startswith(b"%PDF")
    # Nothing half-written is left beside it.
    assert [p.name for p in backups.dir.iterdir() if p.name.startswith(".") and p.name != ".lock"] == []
    assert [b["name"] for b in backups.list()] == [info["name"]]


def test_a_backup_restores_into_an_empty_site(site, tmp_path, database_url_2):
    app, design = site
    zip_path = app.state.backups.path(app.state.backups.create()["name"])

    fake = FakeSupabaseStorage()
    settings = Settings(data_dir=tmp_path / "new", app_url="https://jasmin.example", shopify_webhook_secret="s3cret", database_url=database_url_2)
    new = create_app(settings, storage=SupabaseStorage(settings.data_dir, URL, SECRET, "jasmin", transport=fake.transport))
    svc = new.state.services
    try:
        assert restore(zip_path, svc.db, svc.storage, log=lambda _: None, dry_run=True)["tables"] == {}
        assert svc.db.one("SELECT COUNT(*) AS n FROM templates")["n"] == 0

        report = restore(zip_path, svc.db, svc.storage, log=lambda _: None)
        assert all(t["added"] == t["backup"] == (0 if name == "admins" else 1) for name, t in report["tables"].items())
        client, admin = TestClient(new), TestClient(new)
        admin.auth = ("admin", "jasmin")
        t = client.get("/api/templates/wedding-invitation").json()
        assert client.get(f"{t['assets']}/p0-original.jpg").status_code == 200
        assert client.get(f"/api/designs/{design['id']}/thumbnail.jpg").status_code == 200
        orders = admin.get("/admin/api/orders").json()
        pdf = admin.get(f"/admin/api/order-items/{orders[0]['items'][0]['id']}/print.pdf")
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

        # Again: nothing is added twice, and a new order item is numbered after the restored one.
        again = restore(zip_path, svc.db, svc.storage, log=lambda _: None)
        assert all(t["added"] == 0 and t["already_there"] == t["backup"] for t in again["tables"].values())
        svc.db.execute("INSERT INTO order_items (order_id, line_item_id, design_id, title, variant, quantity) VALUES (?,?,?,?,?,?)", (orders[0]["id"], "another", design["id"], "t", "", 1))
        ids = [r["id"] for r in svc.db.all("SELECT id FROM order_items ORDER BY id")]
        assert len(ids) == 2 and ids[1] > ids[0]
    finally:
        svc.db.close()


def test_a_damaged_or_foreign_zip_is_refused(app, tmp_path):
    svc = app.state.services
    bad = tmp_path / "x.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(BackupError):
        restore(bad, svc.db, svc.storage, log=lambda _: None)
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(BackupError):
        restore(bad, svc.db, svc.storage, log=lambda _: None)
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": 1, "created": "x", "files": {"../../etc/passwd": "0"}, "tables": {}}))
    with pytest.raises(BackupError, match="unexpected file paths"):
        restore(bad, svc.db, svc.storage, log=lambda _: None)


def test_only_the_newest_are_kept(app):
    backups = Backups(app.state.services, app.state.backups.dir, keep=3)
    for day in range(1, 7):
        (backups.dir / f"jasmin-backup-202601{day:02d}T030000Z.zip").write_bytes(b"x")
    (backups.dir / "something-else.zip").write_bytes(b"x")
    assert backups.prune() == [f"jasmin-backup-202601{d:02d}T030000Z.zip" for d in (3, 2, 1)]
    assert [b["name"][14:22] for b in backups.list()] == ["20260106", "20260105", "20260104"]
    assert (backups.dir / "something-else.zip").exists()
    assert backups.list()[0]["readable"] is False


def test_two_backups_cannot_run_at_once(app):
    import fcntl

    backups = app.state.backups
    with (backups.dir / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(BackupBusy):
            backups.create()
    assert backups.create()["readable"]


def test_the_daily_backup_is_due_at_its_hour_or_soon_when_stale():
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    fresh = now - timedelta(hours=9)
    assert next_run(now, 3, fresh) == datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc)
    assert next_run(now, 18, fresh) == datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc)
    assert next_run(now, 3, None) == now + timedelta(minutes=2)
    assert next_run(now, 3, now - timedelta(hours=30)) == now + timedelta(minutes=2)


def test_the_admin_makes_lists_and_downloads_a_backup(app, admin, client):
    fill(app)
    assert client.get("/admin/api/backups").status_code == 401
    assert client.post("/admin/api/backups").status_code == 401
    before = admin.get("/admin/api/backups").json()
    assert before == {"running": False, "error": None, "automatic": False, "hourUtc": 3, "keep": 14, "retentionDays": 0, "backups": []}

    assert admin.post("/admin/api/backups").status_code == 202
    app.state.backups.wait(60)
    after = admin.get("/admin/api/backups").json()
    assert after["running"] is False and after["error"] is None and len(after["backups"]) == 1
    name = after["backups"][0]["name"]
    assert after["backups"][0]["tables"]["orders"] == 1 and after["backups"][0]["size"] > 1000

    assert client.get(f"/admin/api/backups/{name}").status_code == 401
    got = admin.get(f"/admin/api/backups/{name}")
    assert got.status_code == 200 and got.headers["content-type"] == "application/zip" and got.content[:2] == b"PK"
    assert admin.get("/admin/api/backups/jasmin-backup-19990101T000000Z.zip").status_code == 404
    assert admin.get("/admin/api/backups/..%2Fjasmin.db").status_code == 404


def test_a_failed_backup_is_reported_and_leaves_nothing_behind(app, admin, monkeypatch):
    backups = app.state.backups
    monkeypatch.setattr(app.state.services.storage, "keys", lambda: (_ for _ in ()).throw(OSError("storage is down")))
    assert admin.post("/admin/api/backups").status_code == 202
    backups.wait(30)
    state = admin.get("/admin/api/backups").json()
    assert state["error"] == "storage is down" and state["backups"] == []
    assert [p.name for p in backups.dir.iterdir() if p.name != ".lock"] == []


def test_the_schedule_makes_a_backup_when_none_exists_and_not_a_second_one(app, monkeypatch):
    import app.backup as backup_module
    from app.backup import Scheduler

    backups = app.state.backups
    # "Soon" is at once here, so the test does not wait two minutes.
    monkeypatch.setattr(backup_module, "next_run", lambda now, hour, newest: now if newest is None else now + timedelta(hours=20))
    schedule = Scheduler(backups, 3)
    schedule.start()
    try:
        for _ in range(100):
            if backups.list() and not backups.running:
                break
            schedule.join(0.1)
        made = backups.list()
        assert len(made) == 1 and made[0]["kind"] == "automatic"
        schedule.join(1.5)
        assert len(backups.list()) == 1 and schedule.due > datetime.now(timezone.utc) + timedelta(hours=19)
    finally:
        schedule.stop()
        schedule.join(5)
    assert not schedule.is_alive()


def test_added_admins_are_in_a_backup_and_an_older_backup_without_them_still_restores(app, tmp_path, database_url_2):
    from app.db import now

    svc, backups = app.state.services, app.state.backups
    svc.db.execute("INSERT INTO admins (email, added_by, created_at) VALUES (?,?,?)", ("helper@example.com", "jasmin@example.com", now()))
    zip_path = backups.path(backups.create()["name"])
    with zipfile.ZipFile(zip_path) as z:
        assert json.loads(z.read("database/admins.json"))[0]["email"] == "helper@example.com"
        # The same backup as one made before admins existed: without that file.
        older = tmp_path / "older.zip"
        with zipfile.ZipFile(older, "w") as out:
            for name in z.namelist():
                if name != "database/admins.json":
                    out.writestr(name, z.read(name))
    new = create_app(Settings(data_dir=tmp_path / "new", database_url=database_url_2))
    try:
        report = restore(older, new.state.services.db, new.state.services.storage, log=lambda _: None)
        assert report["tables"]["admins"] == {"backup": 0, "added": 0, "already_there": 0}
        assert restore(zip_path, new.state.services.db, new.state.services.storage, log=lambda _: None)["tables"]["admins"]["added"] == 1
    finally:
        new.state.services.db.close()
