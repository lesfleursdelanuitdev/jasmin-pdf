"""Housekeeping: what customers left behind goes after a number of days; what was approved or ordered never does."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.storage import SupabaseStorage

from .conftest import PHOTOS
from .fake_supabase import SECRET, FakeSupabaseStorage
from .test_api import approved_design, order_payload

URL = "https://proj.supabase.co"


def ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


@pytest.fixture(params=["local", "supabase"])
def site(request, settings):
    fake = FakeSupabaseStorage()
    storage = SupabaseStorage(settings.data_dir, URL, SECRET, "jasmin", transport=fake.transport) if request.param == "supabase" else None
    app = create_app(settings, storage=storage)
    from scripts.seed import seed

    seed(app.state.services)
    yield app, (fake if request.param == "supabase" else None)
    app.state.services.db.close()


def upload(client) -> dict:
    with (PHOTOS / "dusk-portrait.jpg").open("rb") as fh:
        return client.post("/api/uploads", files={"file": ("p.jpg", fh, "image/jpeg")}).json()


def draft(client, photo: dict | None = None) -> dict:
    values = {"couple-names": {"text": "A & B"}}
    if photo:
        values["couple-photo"] = {"upload": photo["id"]}
    return client.post("/api/designs", json={"templateId": "wedding-invitation", "values": values}).json()


def test_old_leftovers_go_and_everything_in_use_stays(site):
    app, fake = site
    svc, client = app.state.services, TestClient(app)
    db = svc.db
    age = lambda table, ident, days, col: db.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (ago(days), ident))

    lonely_old = upload(client)                    # uploaded, never used: goes
    lonely_new = upload(client)                    # the same, but recent: stays
    old_draft_photo = upload(client)
    old_draft = draft(client, old_draft_photo)     # abandoned draft and its photo: both go
    client.post(f"/api/designs/{old_draft['id']}/proof")
    fresh_draft_photo = upload(client)
    fresh_draft = draft(client, fresh_draft_photo)  # still being worked on, with an old photo: both stay
    approved = approved_design(client)             # approved long ago: stays, with its photo and proof
    ordered = approved_design(client)
    svc.record_order(order_payload(ordered["id"]), source="demo")
    svc.generate_print_files(str(order_payload(ordered["id"])["id"]))

    for u in (lonely_old, old_draft_photo, fresh_draft_photo):
        age("uploads", u["id"], 30, "created_at")
    for row in db.all("SELECT id FROM uploads"):
        if row["id"] not in (lonely_new["id"],):
            age("uploads", row["id"], 30, "created_at")
    age("designs", old_draft["id"], 30, "updated_at")
    for d in (approved, ordered):
        age("designs", d["id"], 300, "updated_at")

    assert svc.cleanup(20, dry_run=True) == {"drafts": 1, "uploads": 2}
    assert db.one("SELECT COUNT(*) AS n FROM designs")["n"] == 4  # a dry run removes nothing
    assert svc.cleanup(20) == {"drafts": 1, "uploads": 2}

    left = {r["id"] for r in db.all("SELECT id FROM designs")}
    assert left == {fresh_draft["id"], approved["id"], ordered["id"]}
    photos = {r["id"] for r in db.all("SELECT id FROM uploads")}
    assert lonely_old["id"] not in photos and old_draft_photo["id"] not in photos
    assert {lonely_new["id"], fresh_draft_photo["id"]} <= photos and len(photos) == 4

    # Gone for good: no files in the data directory, none in the bucket.
    assert not svc.design_dir(old_draft["id"]).exists()
    assert not list(svc.storage.local("uploads").glob(f"{lonely_old['id']}*"))
    if fake is not None:
        keys = fake.keys()
        assert not [k for k in keys if old_draft["id"] in k or lonely_old["id"] in k or old_draft_photo["id"] in k]
        assert [k for k in keys if approved["id"] in k] and [k for k in keys if k.startswith("print/")]

    # What stays still works: the approved proof, the ordered print file, the fresh draft.
    assert client.get(f"/api/designs/{approved['id']}/proof.pdf").status_code == 200
    assert client.post(f"/api/designs/{fresh_draft['id']}/proof").json()["errors"] == []
    admin = TestClient(app)
    admin.auth = ("admin", "jasmin")
    item = admin.get("/admin/api/orders").json()[0]["items"][0]
    assert admin.get(f"/admin/api/order-items/{item['id']}/print.pdf").status_code == 200
    assert admin.post(f"/admin/api/order-items/{item['id']}/regenerate").json()["status"] == "ready"
    assert client.get(f"/api/designs/{old_draft['id']}").status_code == 404

    assert svc.cleanup(20) == {"drafts": 0, "uploads": 0}  # nothing more to do


def test_nothing_is_removed_when_no_number_of_days_is_set(site):
    app, _ = site
    svc, client = app.state.services, TestClient(app)
    d = draft(client, upload(client))
    svc.db.execute("UPDATE designs SET updated_at=? WHERE id=?", (ago(400), d["id"]))
    svc.db.execute("UPDATE uploads SET created_at=?", (ago(400),))
    assert svc.cleanup(0) == {"drafts": 0, "uploads": 0}
    assert svc.db.one("SELECT COUNT(*) AS n FROM designs")["n"] == 1 and svc.db.one("SELECT COUNT(*) AS n FROM uploads")["n"] == 1


def test_the_daily_schedule_cleans_up_before_it_backs_up(app, monkeypatch):
    import app.backup as backup_module
    from app.backup import Scheduler

    backups, order = app.state.backups, []
    monkeypatch.setattr(backup_module, "next_run", lambda now, hour, newest: now if newest is None else now + timedelta(hours=20))
    monkeypatch.setattr(backups, "create", lambda kind="manual": order.append("backup") or (_ for _ in ()).throw(RuntimeError("stop here")))
    schedule = Scheduler(backups, 3, before=lambda: order.append("cleanup"))
    schedule.start()
    try:
        for _ in range(100):
            if len(order) >= 2:
                break
            schedule.join(0.05)
        assert order[:2] == ["cleanup", "backup"]
    finally:
        schedule.stop()
        schedule.join(5)


def test_the_admin_is_told_about_housekeeping_and_the_demo_store(settings, tmp_path):
    settings.backup_auto, settings.retention_days, settings.demo_store = True, 20, False
    app = create_app(settings)
    try:
        admin = TestClient(app, follow_redirects=False)
        admin.auth = ("admin", "jasmin")
        assert admin.get("/admin/api/backups").json()["retentionDays"] == 20
        assert admin.get("/admin/api/me").json()["demoStore"] is False
        assert admin.get("/demo-store").status_code == 404
        assert admin.get("/").headers["location"] == "/admin"
    finally:
        app.state.services.db.close()
