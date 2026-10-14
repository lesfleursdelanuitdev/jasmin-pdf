"""scripts.copy_data: an existing data directory arrives complete, twice is harmless, the source is untouched."""
from __future__ import annotations

import hashlib
import shutil

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.shopify import sign
from app.storage import SupabaseStorage
from scripts.copy_data import CopyError, copy_data
from scripts.seed import seed

from .fake_supabase import SECRET, FakeSupabaseStorage
from .test_api import approved_design, order_payload

URL = "https://proj.supabase.co"


def digest(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def old(tmp_path):
    """A data directory as the live site has it: SQLite and local files, with a template, a design and an order."""
    import json

    settings = Settings(data_dir=tmp_path / "old", app_url="https://jasmin.example", shopify_webhook_secret="s3cret")
    app = create_app(settings)
    client = TestClient(app)
    seed(app.state.services)
    design = approved_design(client)
    body = json.dumps(order_payload(design["id"])).encode()
    r = client.post("/webhooks/shopify/orders-create", content=body, headers={"X-Shopify-Hmac-Sha256": sign(body, "s3cret"), "Content-Type": "application/json"})
    assert r.status_code == 200
    app.state.services.db.close()
    return tmp_path / "old", design


def test_everything_arrives_and_the_source_is_untouched(old, settings, tmp_path):
    source, design = old
    before = digest(source)
    fake = FakeSupabaseStorage()
    dest = create_app(settings, storage=SupabaseStorage(settings.data_dir, URL, SECRET, "jasmin", transport=fake.transport))
    db = dest.state.services.db
    report = copy_data(source, db, SupabaseStorage(source, URL, SECRET, "jasmin", transport=fake.transport), log=lambda _: None)

    assert digest(source) == before
    assert report["absent"] == [] and report["files"]["missing"] == []
    assert report["tables"]["templates"]["added"] == 1 and report["tables"]["order_items"]["added"] == 1
    assert report["files"]["source"] == report["files"]["stored"] > 10

    # The new app, whose own data directory starts empty, serves what the old one held.
    admin = TestClient(dest)
    admin.auth = ("admin", "jasmin")
    client = TestClient(dest)
    t = client.get("/api/templates/wedding-invitation").json()
    assert client.get(f"{t['assets']}/p0-original.jpg").status_code == 200
    assert client.get(f"/api/designs/{design['id']}/thumbnail.jpg").status_code == 200
    orders = admin.get("/admin/api/orders").json()
    assert len(orders) == 1 and orders[0]["items"][0]["status"] == "ready"
    pdf = admin.get(f"/admin/api/order-items/{orders[0]['items'][0]['id']}/print.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    # A second run adds nothing, and a new order item is numbered after the copied ones.
    again = copy_data(source, db, SupabaseStorage(source, URL, SECRET, "jasmin", transport=fake.transport), log=lambda _: None)
    assert all(t["added"] == 0 and t["already_there"] == t["source"] for t in again["tables"].values())
    db.execute("INSERT INTO order_items (order_id, line_item_id, design_id, title, variant, quantity) VALUES (?,?,?,?,?,?)", (orders[0]["id"], "another", design["id"], "t", "", 1))
    ids = [r["id"] for r in db.all("SELECT id FROM order_items ORDER BY id")]
    assert len(ids) == 2 and ids[1] > ids[0]


def test_a_dry_run_copies_nothing(old, settings):
    source, _ = old
    fake = FakeSupabaseStorage()
    dest = create_app(settings, storage=SupabaseStorage(settings.data_dir, URL, SECRET, "jasmin", transport=fake.transport))
    report = copy_data(source, dest.state.services.db, SupabaseStorage(source, URL, SECRET, "jasmin", transport=fake.transport), log=lambda _: None, dry_run=True)
    assert report["files"]["source"] > 10 and report["tables"] == {}
    assert dest.state.services.db.one("SELECT COUNT(*) AS n FROM templates")["n"] == 0
    assert [k for k in fake.keys() if not k.startswith("fonts/")] == []


def test_a_directory_without_a_database_is_refused(settings, tmp_path):
    (tmp_path / "empty").mkdir()
    dest = create_app(settings)
    with pytest.raises(CopyError):
        copy_data(tmp_path / "empty", dest.state.services.db, None, log=lambda _: None)
