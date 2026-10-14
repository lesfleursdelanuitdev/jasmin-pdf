"""The Account page's server side: an admin changes their own password, and adds and removes admins."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .test_auth import ADMIN, PASSWORD, auth_settings, fake, sign_in  # noqa: F401  (the two fixtures are used by name)

ORIGIN = {"Origin": "https://jasmin.example"}
NEW = "new-admin@example.com"
FIRST = "a first password"


@pytest.fixture
def settings_with_key(auth_settings):
    auth_settings.supabase_secret_key = "test-secret-key"
    # Files stay on disk in these tests: only the accounts talk to (fake) Supabase.
    return auth_settings


@pytest.fixture
def site(settings_with_key, fake):
    from app.main import create_app
    from app.storage import LocalStorage

    app = create_app(settings_with_key, storage=LocalStorage(settings_with_key.data_dir), auth_transport=fake.transport)
    yield app
    app.state.auth.close()
    app.state.services.db.close()


@pytest.fixture
def me(site) -> TestClient:
    client = TestClient(site, base_url="https://jasmin.example", follow_redirects=False)
    assert sign_in(client).status_code == 303
    return client


def test_the_account_page_names_me_and_the_owners(me):
    data = me.get("/admin/api/account").json()
    assert data["email"] == ADMIN and data["minPassword"] == 10
    assert [(a["email"], a["owner"]) for a in data["admins"]] == [(ADMIN, True), ("pending@example.com", True)]


def test_i_change_my_password(me, site, fake):
    r = me.post("/admin/api/account/password", json={"current": PASSWORD, "new": "a much better one"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert fake.users[ADMIN]["password"] == "a much better one"
    # I stay signed in; the old password no longer signs anyone in, the new one does.
    assert me.get("/admin/api/templates").status_code == 200
    fresh = TestClient(site, base_url="https://jasmin.example", follow_redirects=False)
    assert sign_in(fresh).status_code == 401
    assert sign_in(fresh, password="a much better one").status_code == 303


@pytest.mark.parametrize(
    ("current", "new", "message"),
    [
        ("wrong password", "a much better one", "The current password is not right."),
        (PASSWORD, "short", "The password must be at least 10 characters long."),
        (PASSWORD, PASSWORD, "The new password is the same as the current one."),
    ],
)
def test_a_password_change_that_is_refused_says_why(me, fake, current, new, message):
    r = me.post("/admin/api/account/password", json={"current": current, "new": new}, headers=ORIGIN)
    assert r.status_code == 422 and r.json()["detail"] == message
    assert fake.users[ADMIN]["password"] == PASSWORD


def test_i_add_an_admin_who_can_then_sign_in_and_change_their_password(me, site, fake):
    r = me.post("/admin/api/admins", json={"email": "New-Admin@Example.com ", "password": FIRST}, headers=ORIGIN)
    assert r.status_code == 200
    added = r.json()["admins"][-1]
    assert (added["email"], added["owner"], added["addedBy"]) == (NEW, False, ADMIN) and added["createdAt"]
    assert fake.users[NEW]["confirmed"] is True

    them = TestClient(site, base_url="https://jasmin.example", follow_redirects=False)
    assert sign_in(them, email=NEW, password=FIRST).status_code == 303
    assert them.get("/admin/api/templates").status_code == 200
    assert them.get("/admin/api/account").json()["email"] == NEW
    assert them.post("/admin/api/account/password", json={"current": FIRST, "new": "their own password"}, headers=ORIGIN).status_code == 200
    assert fake.users[NEW]["password"] == "their own password"


@pytest.mark.parametrize(
    ("email", "password", "message"),
    [
        ("not an address", FIRST, "That does not look like an email address."),
        (ADMIN, FIRST, f"{ADMIN} is already an admin."),
        (NEW, "short", "The password must be at least 10 characters long."),
    ],
)
def test_adding_an_admin_that_is_refused_says_why(me, fake, email, password, message):
    r = me.post("/admin/api/admins", json={"email": email, "password": password}, headers=ORIGIN)
    assert r.status_code == 422 and r.json()["detail"] == message
    assert NEW not in fake.users


def test_an_address_cannot_be_added_twice(me):
    assert me.post("/admin/api/admins", json={"email": NEW, "password": FIRST}, headers=ORIGIN).status_code == 200
    r = me.post("/admin/api/admins", json={"email": NEW, "password": FIRST}, headers=ORIGIN)
    assert r.status_code == 422 and r.json()["detail"] == f"{NEW} is already an admin."


def test_an_address_that_already_has_an_account_gets_the_password_given(me, site, fake):
    fake.add(NEW, "made in the dashboard")
    assert me.post("/admin/api/admins", json={"email": NEW, "password": FIRST}, headers=ORIGIN).status_code == 200
    assert fake.users[NEW]["password"] == FIRST
    them = TestClient(site, base_url="https://jasmin.example", follow_redirects=False)
    assert sign_in(them, email=NEW, password=FIRST).status_code == 303


def test_a_removed_admin_is_out_at_once_and_their_account_is_gone(me, site, fake):
    me.post("/admin/api/admins", json={"email": NEW, "password": FIRST}, headers=ORIGIN)
    them = TestClient(site, base_url="https://jasmin.example", follow_redirects=False)
    assert sign_in(them, email=NEW, password=FIRST).status_code == 303
    assert them.get("/admin/api/templates").status_code == 200

    r = me.delete(f"/admin/api/admins/{NEW}", headers=ORIGIN)
    assert r.status_code == 200 and [a["email"] for a in r.json()["admins"]] == [ADMIN, "pending@example.com"]
    assert NEW not in fake.users
    # Their session stops working with the very next request, and they cannot sign in again.
    assert them.get("/admin/api/templates").status_code == 401
    assert sign_in(them, email=NEW, password=FIRST).status_code == 401


def test_owners_and_oneself_cannot_be_removed(me, fake):
    me.post("/admin/api/admins", json={"email": NEW, "password": FIRST}, headers=ORIGIN)
    r = me.delete(f"/admin/api/admins/{ADMIN}", headers=ORIGIN)
    assert r.status_code == 422 and r.json()["detail"] == "You cannot remove yourself."
    r = me.delete("/admin/api/admins/pending@example.com", headers=ORIGIN)
    assert r.status_code == 422 and "is an owner" in r.json()["detail"]
    r = me.delete("/admin/api/admins/nobody@example.com", headers=ORIGIN)
    assert r.status_code == 422 and r.json()["detail"] == "nobody@example.com is not an admin."
    assert {ADMIN, "pending@example.com", NEW} <= set(fake.users)  # nobody was deleted


def test_account_routes_need_a_sign_in_and_the_apps_own_origin(site, me):
    anyone = TestClient(site, base_url="https://jasmin.example", follow_redirects=False)
    assert anyone.get("/admin/api/account").status_code == 401
    assert anyone.post("/admin/api/admins", json={"email": NEW, "password": FIRST}, headers=ORIGIN).status_code == 401
    assert anyone.post("/admin/api/account/password", json={"current": PASSWORD, "new": "x" * 12}, headers=ORIGIN).status_code == 401
    assert me.post("/admin/api/admins", json={"email": NEW, "password": FIRST}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_wrong_current_passwords_are_throttled(me):
    for _ in range(10):
        assert me.post("/admin/api/account/password", json={"current": "nope", "new": "a much better one"}, headers=ORIGIN).status_code == 422
    assert me.post("/admin/api/account/password", json={"current": "nope", "new": "a much better one"}, headers=ORIGIN).status_code == 429


def test_without_supabase_there_is_no_account_page(admin):
    assert admin.get("/admin/api/account").status_code == 404
    assert admin.get("/admin/api/me").json() == {"email": None, "demoStore": True}


def test_the_sign_in_service_being_down_is_said_plainly(me, fake):
    fake.fail_with = 500
    r = me.post("/admin/api/admins", json={"email": NEW, "password": FIRST}, headers=ORIGIN)
    assert r.status_code == 503 and "cannot be reached" in r.json()["detail"]
