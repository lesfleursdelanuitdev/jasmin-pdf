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
def test_tables_are_closed_to_everyone_but_their_owner(svc):
    rows = svc.db.all(
        "SELECT c.relname AS t, c.relrowsecurity AS rls FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
        " WHERE n.nspname=current_schema() AND c.relkind='r'"
    )
    assert len(rows) == 5 and all(r["rls"] for r in rows)


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
