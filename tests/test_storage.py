"""File storage: the data directory written through to Supabase Storage (a fake of its API, see fake_supabase)."""
from __future__ import annotations

import json
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from app import storage as storage_module
from app.config import ROOT, Settings
from app.main import create_app
from app.shopify import sign
from app.storage import LocalStorage, StorageError, SupabaseStorage, open_storage
from scripts.seed import seed

from .fake_supabase import SECRET, FakeSupabaseStorage
from .test_api import approved_design, order_payload


@pytest.fixture
def fake():
    return FakeSupabaseStorage()


@pytest.fixture
def store(tmp_path, fake):
    s = SupabaseStorage(tmp_path / "data", "https://proj.supabase.co", SECRET, "jasmin", transport=fake.transport)
    s.ensure_bucket()
    yield s
    s.close()


def write(store, rel, data=b"x"):
    path = store.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_bucket_is_created_private_once(tmp_path, fake):
    s = SupabaseStorage(tmp_path, "https://proj.supabase.co/", SECRET, "jasmin", transport=fake.transport)
    s.ensure_bucket()
    assert fake.buckets == {"jasmin": {"id": "jasmin", "name": "jasmin", "public": False}}
    s.ensure_bucket()
    assert [r for r in fake.requests if r == ("POST", "/storage/v1/bucket")] == [("POST", "/storage/v1/bucket")]


def test_bucket_created_by_someone_else_in_the_meantime_is_fine(tmp_path, fake):
    s = SupabaseStorage(tmp_path, "https://proj.supabase.co", SECRET, "jasmin", transport=fake.transport)
    seen = []

    def racing(request):
        seen.append(request.method)
        if request.method == "GET":
            return httpx.Response(400, json={"statusCode": "404", "error": "Bucket not found", "message": "Bucket not found"})
        return httpx.Response(400, json={"statusCode": "409", "error": "Duplicate", "message": "The resource already exists"})

    s._http = httpx.Client(base_url="https://proj.supabase.co/storage/v1", transport=httpx.MockTransport(racing))
    s.ensure_bucket()
    assert seen == ["GET", "POST"]


def test_round_trip_and_upsert(store, fake):
    write(store, "pdfs/a b.pdf", b"%PDF one")
    store.put("pdfs/a b.pdf")
    assert fake.objects[("jasmin", "pdfs/a b.pdf")] == (b"%PDF one", "application/pdf")
    assert ("POST", "/storage/v1/object/jasmin/pdfs/a b.pdf") in fake.requests

    write(store, "pdfs/a b.pdf", b"%PDF two")
    store.put("pdfs/a b.pdf")  # same key again: replaced, not refused
    (store.root / "pdfs/a b.pdf").unlink()
    assert store.ensure("pdfs/a b.pdf") is True
    assert (store.root / "pdfs/a b.pdf").read_bytes() == b"%PDF two"
    assert [p.name for p in (store.root / "pdfs").iterdir()] == ["a b.pdf"]  # no temp file left behind


def test_ensure_prefers_the_local_file_and_reports_what_exists_nowhere(store, fake):
    write(store, "uploads/local.jpg")
    before = len(fake.requests)
    assert store.ensure("uploads/local.jpg") is True
    assert len(fake.requests) == before  # no request for a file that is already here
    assert store.ensure("uploads/nope.jpg") is False
    assert not (store.root / "uploads/nope.jpg").exists()
    assert store.exists("uploads/local.jpg") is True and store.exists("uploads/nope.jpg") is False


def test_paths_that_leave_the_data_directory_are_refused(store, fake):
    fake.objects[("jasmin", "secret.txt")] = (b"s", "text/plain")
    before = len(fake.requests)
    for rel in ("../secret.txt", "templates/../../secret.txt", "", "/", "templates/./x"):
        assert store.ensure(rel) is False
        assert store.ensure_tree(rel) is False
    assert len(fake.requests) == before
    with pytest.raises(StorageError):
        store.put("../secret.txt")


def test_tree_round_trip_lists_every_level_and_page(store, fake, monkeypatch):
    monkeypatch.setattr(storage_module, "LIST_PAGE", 3)  # several pages per folder
    names = [f"p0-seg{i}.png" for i in range(7)] + ["deep/er/still.png", "p0-original.jpg"]
    for n in names:
        write(store, f"templates/t1/r1/{n}", n.encode())
    write(store, "templates/t1/r2/other.png")
    store.put_tree("templates/t1/r1")
    assert fake.keys() == sorted(f"templates/t1/r1/{n}" for n in names)

    shutil.rmtree(store.root / "templates")
    assert store.ensure_tree("templates/t1/r1") is True
    got = sorted(p.relative_to(store.root / "templates/t1/r1").as_posix() for p in (store.root / "templates/t1/r1").rglob("*") if p.is_file())
    assert got == sorted(names)
    assert (store.root / "templates/t1/r1/deep/er/still.png").read_bytes() == b"deep/er/still.png"
    assert store.ensure_tree("templates/t1/r9") is False

    # A directory that holds only some of its files (fetched one at a time) is completed.
    shutil.rmtree(store.root / "templates")
    assert store.ensure("templates/t1/r1/p0-seg0.png") is True
    assert store.ensure_tree("templates/t1/r1") is True
    assert len([p for p in (store.root / "templates/t1/r1").rglob("*") if p.is_file()]) == len(names)
    assert not [p for p in (store.root / "templates").rglob("*.part")]  # no temp file left behind
    assert store.exists("templates/t1/r1/p0-seg6.png") is True


def test_sync_down_fetches_only_what_is_missing(store, fake):
    fake.objects[("jasmin", "fonts/A.ttf")] = (b"remote A", "font/ttf")
    fake.objects[("jasmin", "fonts/B.ttf")] = (b"remote B", "font/ttf")
    write(store, "fonts/A.ttf", b"local A")
    store.sync_down("fonts")
    assert (store.root / "fonts/A.ttf").read_bytes() == b"local A"
    assert (store.root / "fonts/B.ttf").read_bytes() == b"remote B"


def test_delete_tree_removes_everything_below_and_nothing_else(store, fake, monkeypatch):
    monkeypatch.setattr(storage_module, "DELETE_BATCH", 2)
    for rel in ("templates/t1/r1/a.png", "templates/t1/r1/b.png", "templates/t1/r2/x/c.png", "templates/t10/r1/a.png", "pdfs/k.pdf"):
        write(store, rel)
        store.put(rel)
    store.delete_tree("templates/t1")
    assert fake.keys() == ["pdfs/k.pdf", "templates/t10/r1/a.png"]
    assert (store.root / "templates/t1/r1/a.png").exists()  # the local copy is the caller's business
    store.delete_tree("templates/none")  # nothing there: no error


def test_one_retry_on_a_server_error_then_it_gives_up(store, fake, monkeypatch):
    monkeypatch.setattr(storage_module.time, "sleep", lambda _: None)
    write(store, "pdfs/r.pdf", b"%PDF")
    fake.fail_next = [503]
    store.put("pdfs/r.pdf")
    assert fake.keys() == ["pdfs/r.pdf"]
    fake.fail_next = [502, 502]
    with pytest.raises(StorageError, match="HTTP 502"):
        store.put("pdfs/r.pdf")


def test_one_retry_on_a_connection_error(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(storage_module.time, "sleep", lambda _: None)
    calls = []

    def flaky(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ConnectError("no route", request=request)
        return fake.handle(request)

    fake.buckets["jasmin"] = {"id": "jasmin"}
    s = SupabaseStorage(tmp_path, "https://proj.supabase.co", SECRET, transport=httpx.MockTransport(flaky))
    write(s, "pdfs/c.pdf")
    s.put("pdfs/c.pdf")
    assert len(calls) == 2 and fake.keys() == ["pdfs/c.pdf"]

    def down(request):
        raise httpx.ConnectError("no route", request=request)

    s._http = httpx.Client(base_url="https://proj.supabase.co/storage/v1", transport=httpx.MockTransport(down))
    with pytest.raises(StorageError, match="unreachable"):
        s.ensure("pdfs/other.pdf")


def test_a_refused_request_is_an_error_not_a_missing_file(tmp_path, fake):
    fake.buckets["jasmin"] = {"id": "jasmin"}
    s = SupabaseStorage(tmp_path, "https://proj.supabase.co", "wrong-key", transport=fake.transport)
    write(s, "pdfs/x.pdf")
    with pytest.raises(StorageError, match="HTTP 403"):
        s.put("pdfs/x.pdf")
    with pytest.raises(StorageError, match="HTTP 403"):
        s.ensure("pdfs/y.pdf")
    assert len([r for r in fake.requests if r[0] == "POST"]) == 1  # a 4xx is not retried


def test_settings_choose_the_backend(tmp_path):
    assert isinstance(open_storage(Settings(data_dir=tmp_path)), LocalStorage)
    assert isinstance(open_storage(Settings(data_dir=tmp_path, supabase_url="https://p.supabase.co")), LocalStorage)
    s = open_storage(Settings(data_dir=tmp_path, supabase_url="https://p.supabase.co", supabase_secret_key="k", storage_bucket="b"))
    assert isinstance(s, SupabaseStorage) and s.bucket == "b"
    s.close()


def test_local_storage_changes_nothing(tmp_path):
    s = LocalStorage(tmp_path)
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs/a.pdf").write_bytes(b"x")
    assert s.ensure("pdfs/a.pdf") and s.exists("pdfs/a.pdf") and s.ensure_tree("pdfs")
    assert not s.ensure("pdfs/b.pdf") and not s.ensure_tree("nope")
    for call in (lambda: s.put("pdfs/a.pdf"), lambda: s.put_tree("pdfs"), lambda: s.delete_tree("pdfs"), lambda: s.sync_down("pdfs"), s.close):
        assert call() is None
    assert (tmp_path / "pdfs/a.pdf").read_bytes() == b"x"


# ---------------------------------------------------------------------------
# The app on storage: the data directory is only a cache
# ---------------------------------------------------------------------------

def post_order(client, payload):
    body = json.dumps(payload).encode()
    return client.post("/webhooks/shopify/orders-create", content=body, headers={"X-Shopify-Hmac-Sha256": sign(body, "s3cret")})


def make_app(settings, fake):
    storage = SupabaseStorage(settings.data_dir, "https://proj.supabase.co", SECRET, "jasmin", transport=fake.transport)
    return create_app(settings, storage)


def test_app_serves_everything_again_after_the_data_directory_is_emptied(settings, fake):
    app = make_app(settings, fake)
    svc = app.state.services
    client = TestClient(app)
    admin = TestClient(app)
    admin.auth = ("admin", "jasmin")
    try:
        template = seed(svc)
        design = approved_design(client)
        design_id = design["id"]
        assert post_order(client, order_payload(design_id)).json() == {"ok": True, "personalized": True}
        order = admin.get("/admin/api/orders/4400000000001").json()
        item = order["items"][0]
        assert item["status"] == "ready", item
        with open(ROOT / "fonts" / "Montserrat-Regular.ttf", "rb") as fh:
            font = admin.post("/admin/api/fonts", files={"file": ("Montserrat-Regular.ttf", fh, "font/ttf")})
        assert font.status_code == 200, font.text

        payload = client.get("/api/templates/wedding-invitation").json()
        assets = [f"{payload['assets']}/{layer['src']}" for stack in payload["stacks"] for layer in stack["stack"] if layer["kind"] == "static"]
        assets.append(f"{payload['assets']}/p0-original.jpg")
        upload_id = svc.get_design(design_id)["values"]["couple-photo"]["upload"]
        preview_url = client.get(f"/api/uploads/{upload_id}").json()["previewUrl"]
        public = [*assets, preview_url] + [f"/api/designs/{design_id}/{name}" for name in ("thumbnail.jpg", "proof-p0.jpg", "proof.pdf")]
        private = ["/admin/api/templates/wedding-invitation/source.pdf", f"/admin/api/order-items/{item['id']}/print.pdf"]
        before = {}
        for url in public + private:
            resp = (admin if url in private else client).get(url)
            assert resp.status_code == 200, url
            before[url] = resp.content
        assert len(assets) >= 2 and before[f"/admin/api/order-items/{item['id']}/print.pdf"].startswith(b"%PDF")

        # Everything the app wrote is in the bucket, under its path in the data directory.
        on_disk = sorted(
            p.relative_to(settings.data_dir).as_posix()
            for p in settings.data_dir.rglob("*")
            if p.is_file() and not p.name.startswith("jasmin.db")
        )
        assert fake.keys() == on_disk
        tops = {k.split("/")[0] for k in on_disk}
        assert tops == {"pdfs", "templates", "uploads", "designs", "print", "fonts"}
        # One set of template images: the saves so far kept the one the template was made with.
        assert {k.split("/")[2] for k in on_disk if k.startswith("templates/")} == {"r1"}
    finally:
        svc.db.close()
        svc.storage.close()

    # A new volume: nothing left but the database (when it is the SQLite file).
    for entry in settings.data_dir.iterdir():
        if entry.name.startswith("jasmin.db"):
            continue
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()

    app = make_app(settings, fake)
    svc = app.state.services
    client = TestClient(app)
    admin = TestClient(app)
    admin.auth = ("admin", "jasmin")
    try:
        assert (settings.data_dir / "fonts" / "Montserrat-Regular.ttf").is_file()  # uploaded fonts come back at startup
        assert "uploaded" in {f.source for f in svc.fonts.all()}
        for url in public + private:
            resp = (admin if url in private else client).get(url)
            assert resp.status_code == 200, url
            assert resp.content == before[url], url
        assert client.get(f"{payload['assets']}/nope.png").status_code == 404
        assert client.get("/assets/uploads/img_nope-preview.jpg").status_code == 404
        assert client.get(f"/api/designs/{design_id}/proof-p9.jpg").status_code == 404

        # Work goes on from the bucket: a save keeps the images it has, a new proof needs the PDF and the photo.
        stored = fake.keys()
        updated = admin.patch("/admin/api/templates/wedding-invitation", json={"name": "Renamed"})
        assert updated.status_code == 200, updated.text
        assert updated.json()["assets"] == payload["assets"] and fake.keys() == stored
        second = approved_design(client)
        assert client.get(f"/api/designs/{second['id']}/proof.pdf").content.startswith(b"%PDF")
        regenerated = admin.post(f"/admin/api/order-items/{item['id']}/regenerate").json()
        assert regenerated["status"] == "ready", regenerated

        # Deleting a template removes its assets from the bucket too.
        assert admin.delete("/admin/api/templates/wedding-invitation").status_code == 200
        assert not [k for k in fake.keys() if k.startswith(f"templates/{template['id']}/")]
    finally:
        svc.db.close()
        svc.storage.close()


def test_storage_trouble_is_reported_not_hidden(settings, fake, monkeypatch):
    monkeypatch.setattr(storage_module.time, "sleep", lambda _: None)
    app = make_app(settings, fake)
    svc = app.state.services
    client = TestClient(app)
    try:
        seed(svc)
        fake.fail_next = [500, 500]
        with open(ROOT / "samples" / "photos" / "dusk-portrait.jpg", "rb") as fh:
            resp = client.post("/api/uploads", files={"file": ("dusk-portrait.jpg", fh, "image/jpeg")})
        assert resp.status_code == 503 and "storage" in resp.json()["detail"].lower()
        assert svc.db.all("SELECT id FROM uploads") == []  # no row for a photo that was not stored
    finally:
        svc.db.close()
        svc.storage.close()


def test_without_supabase_nothing_leaves_the_data_directory(app, svc, client, template):
    assert isinstance(svc.storage, LocalStorage)
    design = approved_design(client)
    assert client.get(f"/api/designs/{design['id']}/thumbnail.jpg").status_code == 200
    payload = client.get("/api/templates/wedding-invitation").json()
    assert client.get(f"{payload['assets']}/p0-original.jpg").status_code == 200
    assert client.get(f"{payload['assets']}/nope.png").status_code == 404


# -- the path-handing interface -------------------------------------------------
def test_files_are_reached_through_storage_and_nowhere_else():
    """Nothing but the storage module builds paths into the data directory, so nothing can skip storing or fetching."""
    import re

    allowed = {
        "app/services.py": 2,  # makes the directory, and names the SQLite file in it
        "app/main.py": 1,      # the default place for backups
    }
    for path in sorted((ROOT / "app").glob("**/*.py")) + sorted((ROOT / "scripts").glob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text()
        assert ".dirs[" not in text, rel
        if rel in ("app/storage.py", "app/config.py"):
            continue
        uses = len(re.findall(r"data_dir\b", text))
        assert uses <= allowed.get(rel, 0) or rel.startswith("scripts/"), f"{rel} uses the data directory {uses} times"


def test_reading_fetches_writing_stores_removing_removes_both(store, fake, tmp_path):
    from app.storage import Missing

    with store.writing("uploads/a.jpg") as path:
        path.write_bytes(b"photo")
    store.write("print/9/b.pdf", b"%PDF")
    assert fake.keys() == ["print/9/b.pdf", "uploads/a.jpg"]

    # A second app with an empty data directory gets the files when it asks for their paths.
    other = SupabaseStorage(tmp_path / "other", "https://proj.supabase.co", SECRET, "jasmin", transport=fake.transport)
    assert other.path("uploads/a.jpg").read_bytes() == b"photo" and other.has("print/9/b.pdf")
    with pytest.raises(Missing):
        other.path("uploads/nope.jpg")
    with pytest.raises(Missing):
        other.path("../outside")

    with store.writing_tree("templates/t/r1") as out:
        (out / "p0.jpg").write_bytes(b"one")
        (out / "sub").mkdir()
        (out / "sub" / "x.png").write_bytes(b"two")
    assert store.copy_tree("templates/t/r1", "templates/t/r2") and not store.copy_tree("templates/t/r9", "templates/t/r10")
    assert other.tree("templates/t/r2") is not None and (other.local("templates/t/r2") / "sub" / "x.png").read_bytes() == b"two"
    assert other.tree("templates/none") is None

    # Written again with clear: what was there before is gone, in both places.
    with store.writing_tree("templates/t/r1") as out:
        (out / "only.jpg").write_bytes(b"new")
    assert [k for k in fake.keys() if k.startswith("templates/t/r1/")] == ["templates/t/r1/only.jpg"]

    store.remove(["uploads/a.jpg"])
    store.remove_tree("templates/t")
    assert fake.keys() == ["print/9/b.pdf"] and not store.local("uploads/a.jpg").exists() and not store.local("templates/t").exists()


def test_a_file_that_failed_to_be_written_is_not_stored(store, fake):
    with pytest.raises(RuntimeError):
        with store.writing("uploads/half.jpg") as path:
            path.write_bytes(b"half")
            raise RuntimeError("the encoder fell over")
    assert fake.keys() == []


def test_a_print_file_that_is_gone_is_a_404_not_a_crash(app, admin, client, template):
    design = approved_design(client)
    svc = app.state.services
    svc.record_order(order_payload(design["id"]), source="demo")
    svc.generate_print_files(str(order_payload(design["id"])["id"]))
    item = svc.db.one("SELECT * FROM order_items")
    assert admin.get(f"/admin/api/order-items/{item['id']}/print.pdf").status_code == 200
    svc.storage.local(f"print/{item['print_file']}").unlink()
    assert admin.get(f"/admin/api/order-items/{item['id']}/print.pdf").status_code == 404
    svc.storage.local(f"pdfs/{template['pdf_file']}").unlink()
    assert admin.get(f"/admin/api/templates/{template['id']}/source.pdf").status_code == 404
