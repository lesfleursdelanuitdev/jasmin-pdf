"""The database layer: the same behaviour on SQLite and on Postgres (see conftest.database_url)."""
from __future__ import annotations

import pytest

from app.db import Database, PostgresDatabase, to_pg
from app.services import Invalid

from .conftest import SAMPLE_PDF, TEST_DATABASE_URL


def test_backend_follows_the_settings(svc):
    assert isinstance(svc.db, PostgresDatabase if TEST_DATABASE_URL else Database)


def test_placeholders_are_rewritten_outside_quotes():
    assert to_pg("SELECT * FROM t WHERE a=? AND b<>?") == "SELECT * FROM t WHERE a=%s AND b<>%s"
    assert to_pg("SELECT '?' AS q, \"a?b\" FROM t WHERE c=?") == "SELECT '?' AS q, \"a?b\" FROM t WHERE c=%s"
    assert to_pg("SELECT 1 WHERE a LIKE '10%' AND b=?") == "SELECT 1 WHERE a LIKE '10%%' AND b=%s"


def test_rows_are_plain_dicts_with_the_same_types(svc, template):
    row = svc.db.one("SELECT * FROM templates WHERE id=?", (template["id"],))
    assert type(row) is dict
    assert isinstance(row["revision"], int) and isinstance(row["definition"], str) and isinstance(row["created_at"], str)
    assert svc.db.one("SELECT COUNT(*) AS n FROM templates")["n"] == 1
    assert svc.db.one("SELECT * FROM templates WHERE id=?", ("nope",)) is None
    assert [r["id"] for r in svc.db.all("SELECT id FROM templates WHERE status<>?", ("draft",))] == [template["id"]]
    assert svc.db.all("SELECT id FROM templates WHERE name LIKE '%no such%' AND id=?", ("x",)) == []


def test_template_is_found_by_a_linked_product_handle(svc, template):
    svc.update_template(template["id"], {"productHandles": ["Save The Date", "arch-invite"]})
    assert svc.get_template(template["id"])["product_handles"] == ["arch-invite", "save-the-date"]
    assert svc.template_for_product("arch-invite")["id"] == template["id"]
    assert svc.template_for_product("save-the-date")["id"] == template["id"]
    # A partial match is not a match, and the id no longer counts once handles are set.
    assert svc.template_for_product("arch") is None
    assert svc.template_for_product(template["id"]) is None
    assert svc.db.template_for_handle("arch-invite", exclude=template["id"]) is None


def test_template_without_handles_is_found_by_its_id_only_when_published(svc, template):
    assert svc.template_for_product("wedding-invitation")["id"] == template["id"]
    svc.update_template(template["id"], {"status": "draft"})
    assert svc.template_for_product("wedding-invitation") is None
    svc.update_template(template["id"], {"productHandles": ["arch-invite"]})
    assert svc.template_for_product("arch-invite") is None
    assert svc.db.template_for_handle("arch-invite")["id"] == template["id"]


def test_a_product_handle_cannot_be_linked_to_two_templates(svc, template):
    other = svc.create_template("Second", SAMPLE_PDF.read_bytes())
    svc.update_template(template["id"], {"productHandles": ["arch-invite"]})
    with pytest.raises(Invalid, match="already linked to template 'wedding-invitation'"):
        svc.update_template(other["id"], {"productHandles": ["arch-invite"]})
    # Saving the same handle on the template that owns it is fine.
    svc.update_template(template["id"], {"productHandles": ["arch-invite"]})
    svc.update_template(other["id"], {"productHandles": ["other-invite"]})
    assert svc.template_for_product("other-invite") is None  # still a draft
    assert svc.db.template_for_handle("other-invite")["id"] == other["id"]


def test_an_order_sent_twice_keeps_one_row_per_line_item(svc, template):
    design = svc.create_design(template["id"], {})
    payload = {
        "id": 5001,
        "name": "#1001",
        "email": "sofia@example.com",
        "line_items": [
            {"id": 11, "title": "Invitation", "quantity": 2, "properties": [{"name": "_design_id", "value": design["id"]}]},
            {"id": 12, "title": "Invitation", "quantity": 1, "properties": [{"name": "_design_id", "value": design["id"]}]},
        ],
    }
    first = svc.record_order(payload)
    again = svc.record_order(payload)
    assert [i["line_item_id"] for i in again["items"]] == ["11", "12"]
    assert [i["id"] for i in again["items"]] == [i["id"] for i in first["items"]]
    assert [i["quantity"] for i in again["items"]] == [2, 1]
    assert all(isinstance(i["id"], int) for i in again["items"])
    assert svc.db.one("SELECT COUNT(*) AS n FROM orders")["n"] == 1


def test_item_ids_can_be_set(svc, template):
    """Rows copied from another database keep their ids."""
    svc.db.execute(
        "INSERT INTO order_items (id, order_id, line_item_id, quantity) VALUES (?,?,?,?)", (40, "9001", "1", 1)
    )
    assert svc.db.one("SELECT * FROM order_items WHERE id=?", (40,))["status"] == "pending"


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="Postgres only")
def test_a_dropped_connection_is_replaced(svc, template):
    """The pool does not test a connection before use; a statement that finds it dead runs again on a new one."""
    svc.db.one("SELECT 1 AS x")
    with pytest.raises(Exception):
        svc.db.execute("SELECT pg_terminate_backend(pg_backend_pid())")
    assert svc.db.one("SELECT id FROM templates WHERE id=?", (template["id"],))["id"] == template["id"]


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="Postgres only")
def test_every_table_in_the_schema_is_closed_to_everyone_but_its_owner(svc):
    """Not only the tables on the list: a table added to the schema without being listed would fail here."""
    from app.db import INTERNAL_TABLES, TABLES

    rows = svc.db.all(
        "SELECT c.relname AS t, c.relrowsecurity AS rls FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
        " WHERE n.nspname=current_schema() AND c.relkind='r'"
    )
    assert {r["t"] for r in rows} == {*TABLES, *INTERNAL_TABLES}
    assert all(r["rls"] for r in rows)


def test_orders_are_listed_newest_first_with_their_items(svc, template):
    for n, when in ((1, "2026-01-01T00:00:00+00:00"), (2, "2026-02-01T00:00:00+00:00")):
        svc.record_order(
            {"id": f"o{n}", "name": f"#{n}", "created_at": when, "line_items": [{"id": f"l{n}", "title": "t", "properties": [{"name": "_design_id", "value": f"dsn_{n}"}]}]},
            source="demo",
        )
    orders = svc.list_orders()
    assert [o["id"] for o in orders] == ["o2", "o1"]
    assert [o["items"][0]["line_item_id"] for o in orders] == ["l2", "l1"] and "raw" not in orders[0]
    assert orders[0]["items"][0]["thumbnailUrl"] == "/api/designs/dsn_2/thumbnail.jpg"


# -- one table list, migrations, row counts, transactions ------------------------
def test_there_is_one_list_of_tables(svc):
    import app.backup as backup
    import scripts.copy_data as copy_data
    from app.db import TABLES

    assert backup.TABLES is TABLES
    assert set(copy_data.TABLES) == set(TABLES) - {"admins"}
    listed = "sqlite_master WHERE type='table'" if not TEST_DATABASE_URL else "information_schema.tables WHERE table_schema=current_schema()"
    name = "name" if not TEST_DATABASE_URL else "table_name"
    from app.db import INTERNAL_TABLES

    found = {r["n"] for r in svc.db.all(f"SELECT {name} AS n FROM {listed}")} - {"sqlite_sequence"}
    assert found == {*TABLES, *INTERNAL_TABLES}  # what is backed up, and the app's own machinery


def open_db(tmp_path, url, migrations=None):
    from app.db import open_database

    return open_database(tmp_path / "m.db", url, "", migrations)


def test_a_new_database_is_brought_to_the_newest_version(tmp_path, database_url_2):
    from app.db import MIGRATIONS

    db = open_db(tmp_path, database_url_2)
    try:
        assert db.version == MIGRATIONS[-1][0] == db.one("SELECT version FROM schema_version")["version"]
        assert db.one("SELECT COUNT(*) AS n FROM schema_version")["n"] == 1
    finally:
        db.close()


def test_a_migration_runs_once_and_keeps_the_data(tmp_path, database_url_2):
    from app.db import MIGRATIONS

    db = open_db(tmp_path, database_url_2)
    db.execute("INSERT INTO admins (email, added_by, created_at) VALUES (?,?,?)", ("a@example.com", "", "2026-01-01"))
    db.close()
    more = [*MIGRATIONS, (MIGRATIONS[-1][0] + 1, "ALTER TABLE admins ADD COLUMN note TEXT NOT NULL DEFAULT 'none'")]
    for _ in range(2):  # the second start must not try to add the column again
        db = open_db(tmp_path, database_url_2, more)
        assert db.version == more[-1][0]
        assert db.one("SELECT email, note FROM admins") == {"email": "a@example.com", "note": "none"}
        db.close()


def test_a_database_from_before_migrations_is_taken_over_as_it_is(tmp_path, database_url_2):
    """The live database has the tables but no version table: nothing is lost, and it gets a version."""
    db = open_db(tmp_path, database_url_2)
    db.execute("INSERT INTO admins (email, added_by, created_at) VALUES (?,?,?)", ("a@example.com", "", "2026-01-01"))
    db.execute("DROP TABLE schema_version")
    db.close()
    db = open_db(tmp_path, database_url_2)
    try:
        assert db.version >= 1 and db.one("SELECT COUNT(*) AS n FROM admins")["n"] == 1
    finally:
        db.close()


def test_a_migration_that_fails_changes_nothing(tmp_path, database_url_2):
    from app.db import MIGRATIONS

    open_db(tmp_path, database_url_2).close()
    bad = [*MIGRATIONS, (MIGRATIONS[-1][0] + 1, "ALTER TABLE admins ADD COLUMN note TEXT; ALTER TABLE no_such_table ADD COLUMN x TEXT")]
    with pytest.raises(Exception):
        open_db(tmp_path, database_url_2, bad)
    db = open_db(tmp_path, database_url_2)
    try:
        assert db.version == MIGRATIONS[-1][0]
        if TEST_DATABASE_URL:  # Postgres takes the half-done change back too; SQLite cannot undo an ALTER TABLE
            assert "note" not in db.one("SELECT * FROM admins LIMIT 1") if db.one("SELECT * FROM admins LIMIT 1") else True
    finally:
        db.close()


def test_execute_says_how_many_rows_changed(svc, template):
    assert svc.db.execute("UPDATE templates SET name=? WHERE id=?", ("X", template["id"])) == 1
    assert svc.db.execute("UPDATE templates SET name=? WHERE id=?", ("X", "nope")) == 0
    assert svc.db.execute("DELETE FROM templates WHERE id=?", ("nope",)) == 0


def test_a_transaction_is_all_or_nothing(svc):
    db = svc.db
    add = lambda email: db.execute("INSERT INTO admins (email, added_by, created_at) VALUES (?,?,?)", (email, "", "2026-01-01"))
    with db.transaction():
        add("one@example.com")
        add("two@example.com")
        assert db.one("SELECT COUNT(*) AS n FROM admins")["n"] == 2  # seen inside it
    assert db.one("SELECT COUNT(*) AS n FROM admins")["n"] == 2
    with pytest.raises(RuntimeError):
        with db.transaction():
            add("three@example.com")
            with db.transaction():  # one inside another is part of it
                add("four@example.com")
            raise RuntimeError("stop")
    assert db.one("SELECT COUNT(*) AS n FROM admins")["n"] == 2
    assert add("five@example.com") == 1 and db.one("SELECT COUNT(*) AS n FROM admins")["n"] == 3  # back to committing each statement


def test_an_order_is_stored_with_its_items_or_not_at_all(svc, template, monkeypatch):
    payload = {"id": "o1", "name": "#1", "line_items": [{"id": "l1", "title": "t", "quantity": "not a number", "properties": [{"name": "_design_id", "value": "dsn_1"}]}]}
    with pytest.raises(ValueError):
        svc.record_order(payload, source="demo")
    assert svc.db.one("SELECT COUNT(*) AS n FROM orders")["n"] == 0
    payload["line_items"][0]["quantity"] = 2
    assert svc.record_order(payload, source="demo")["items"][0]["quantity"] == 2


def test_an_edit_that_arrives_during_approval_is_not_approved_unseen(client, svc, template, monkeypatch):
    from .test_api import upload

    photo = upload(client)
    d = client.post("/api/designs", json={"templateId": "wedding-invitation", "values": {"couple-photo": {"upload": photo["id"]}, "couple-names": {"text": "Sofia & Mateo"}}}).json()
    real_proof = svc.proof

    def proof_with_an_edit_in_the_middle(design_id):
        result = real_proof(design_id)
        svc.update_design(design_id, {"couple-photo": {"upload": photo["id"]}, "couple-names": {"text": "Sofia & Matteo"}})
        return result

    monkeypatch.setattr(svc, "proof", proof_with_an_edit_in_the_middle)
    r = client.post(f"/api/designs/{d['id']}/approve")
    assert r.status_code == 422 and "changed while the proof was being made" in r.json()["detail"]
    assert svc.get_design(d["id"])["status"] == "draft"
    # Approved again without an edit in the middle, it goes through with the newer wording.
    monkeypatch.setattr(svc, "proof", real_proof)
    assert client.post(f"/api/designs/{d['id']}/approve").json()["status"] == "approved"
    assert "Matteo" in svc.get_design(d["id"])["summary"]


def test_an_approved_design_cannot_be_changed_even_by_a_late_save(client, svc, template):
    from .test_api import approved_design

    d = approved_design(client)
    assert client.put(f"/api/designs/{d['id']}", json={"values": {"couple-names": {"text": "Late"}}}).status_code == 422
    # Straight at the statement, as a save that read "draft" a moment before the approval would:
    assert svc.db.execute('UPDATE designs SET "values"=? WHERE id=? AND status=\'draft\'', ("{}", d["id"])) == 0
    assert "Sofia" in svc.get_design(d["id"])["summary"]
