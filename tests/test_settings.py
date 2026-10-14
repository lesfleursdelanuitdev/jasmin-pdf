"""What a configuration amounts to, and the ones the app refuses to start with."""
from __future__ import annotations

import pytest

from app.config import Settings, describe, load_settings
from app.main import create_app

SUPA = dict(supabase_url="https://p.supabase.co", supabase_publishable_key="pk", supabase_secret_key="sk", admin_emails=["a@example.com"], session_secret="s")


def test_the_modes_are_named_in_one_line():
    modes, warnings, errors = describe(Settings())
    assert modes == "database SQLite, files data directory only, admin sign-in HTTP Basic, Shopify store not connected, demo store on, backups by hand, housekeeping off"
    assert warnings == [] and errors == []
    live = Settings(app_url="https://studio.example", database_url="postgresql://x", backup_auto=True, retention_days=20, **SUPA)
    modes, warnings, errors = describe(live)
    assert modes == "database Postgres, files Supabase Storage, admin sign-in Supabase, Shopify store not connected, demo store on, backups daily, housekeeping after 20 days"
    assert warnings == [] and errors == []


@pytest.mark.parametrize(
    ("settings", "part"),
    [
        (Settings(app_url="https://studio.example"), "default password"),
        (Settings(supabase_url="https://p.supabase.co", supabase_publishable_key="pk"), "half set up (ADMIN_EMAILS missing)"),
        (Settings(admin_emails=["a@example.com"]), "half set up (SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY missing)"),
    ],
)
def test_settings_that_would_leave_the_admin_open_are_errors(settings, part):
    assert part in " ".join(describe(settings)[2])


@pytest.mark.parametrize(
    ("settings", "part"),
    [
        (Settings(**{**SUPA, "supabase_secret_key": ""}), "cannot change passwords"),
        (Settings(**{**SUPA, "session_secret": ""}), "will not survive a restart"),
        (Settings(**SUPA), "SQLite file"),
        (Settings(shopify_shop_domain="s.myshopify.com", shopify_client_id="c", shopify_client_secret="x"), "demo store is on although"),
        (Settings(shopify_client_id="c"), "Only one of"),
        (Settings(app_url="https://studio.example", admin_password="set", demo_store=False), "cannot be verified"),
    ],
)
def test_settings_that_are_probably_a_mistake_are_warnings(settings, part):
    modes, warnings, errors = describe(settings)
    assert part in " ".join(warnings), warnings


def test_a_real_run_refuses_to_start_open_but_settings_made_in_code_only_say_so(tmp_path, monkeypatch, caplog):
    for name in ("APP_URL", "JASMIN_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_URL", "https://studio.example")
    monkeypatch.setenv("JASMIN_DATA_DIR", str(tmp_path / "d"))
    for name in ("ADMIN_PASSWORD", "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "ADMIN_EMAILS", "DATABASE_URL", "SUPABASE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="default password"):
        create_app(load_settings())
    monkeypatch.setenv("ADMIN_PASSWORD", "a real password")
    app = create_app(load_settings())
    app.state.services.db.close()

    in_code = create_app(Settings(data_dir=tmp_path / "e", app_url="https://studio.example"))
    in_code.state.services.db.close()
    assert "default password" in caplog.text


def test_a_template_with_a_missing_font_cannot_be_published_or_proofed(client, svc, template):
    from .test_api import upload

    definition = svc.get_template(template["id"])["definition"]
    definition["elements"][1]["font"] = "NoSuchFont-Regular"
    from app.db import dumps

    svc.db.execute("UPDATE templates SET definition=?, status='draft' WHERE id=?", (dumps(definition), template["id"]))
    r = svc.editor_payload(svc.get_template(template["id"]))
    assert "NoSuchFont-Regular" in r["fonts"]  # still shown on screen, in the stand-in font
    from app.services import Invalid

    with pytest.raises(Invalid, match="NoSuchFont-Regular"):
        svc.update_template(template["id"], {"status": "published"})
    photo = upload(client)
    d = client.post("/api/designs", json={"templateId": template["id"], "values": {"couple-photo": {"upload": photo["id"]}}}).json()
    proof = client.post(f"/api/designs/{d['id']}/proof").json()
    assert proof["pages"] == [] and any("a font is missing" in e for e in proof["errors"])
