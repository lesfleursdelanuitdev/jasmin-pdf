"""Admin sign-in with Supabase Auth, against an in-process fake of its token endpoint."""
from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth as auth_module
from app.auth import COOKIE, AdminAuth
from app.config import Settings, load_settings
from app.main import create_app

from .fake_supabase import PUBLISHABLE, FakeSupabaseAuth

ORIGIN = "https://jasmin.example"
ADMIN = "jasmin@example.com"
PASSWORD = "correct horse"
GENERIC = "That email and password did not work."


@pytest.fixture
def fake():
    f = FakeSupabaseAuth()
    f.add(ADMIN, PASSWORD)
    f.add("visitor@example.com", "visitor pass")
    f.add("pending@example.com", "pending pass", confirmed=False)
    return f


@pytest.fixture
def auth_settings(tmp_path, database_url) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        app_url=ORIGIN,
        database_url=database_url,
        supabase_url="https://proj.supabase.co",
        supabase_publishable_key=PUBLISHABLE,
        admin_emails=[ADMIN, "pending@example.com"],
        session_secret="test-session-secret",
    )


@pytest.fixture
def auth_app(auth_settings, fake):
    app = create_app(auth_settings, auth_transport=fake.transport)
    yield app
    app.state.auth.close()
    app.state.services.db.close()


@pytest.fixture
def browser(auth_app) -> TestClient:
    return TestClient(auth_app, base_url=ORIGIN, follow_redirects=False)


def sign_in(client, email=ADMIN, password=PASSWORD):
    return client.post("/admin/login", data={"email": email, "password": password})


def test_sign_in_sets_the_session_cookie_and_opens_the_admin(browser, fake):
    assert browser.get("/admin/api/templates").status_code == 401
    r = sign_in(browser, email="  Jasmin@Example.com ")
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/admin", "Max-Age=43200"):
        assert attribute in cookie
    assert "fake-access-token" not in cookie and "fake-refresh-token" not in cookie
    assert browser.get("/admin").status_code == 200
    assert browser.get("/admin/api/templates").json() == []
    assert browser.get("/admin/api/me").json()["email"] == ADMIN
    sent = fake.requests[-1]
    assert sent.headers["apikey"] == PUBLISHABLE and b"correct horse" in sent.content


def test_the_cookie_is_not_marked_secure_on_plain_http(auth_settings, fake):
    auth_settings.app_url = "http://localhost:8000"
    app = create_app(auth_settings, auth_transport=fake.transport)
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as c:
        cookie = sign_in(c).headers["set-cookie"]
        assert "Secure" not in cookie and "HttpOnly" in cookie
        assert c.get("/admin/api/me").json()["email"] == ADMIN
    app.state.services.db.close()


@pytest.mark.parametrize(
    "email, password",
    [
        (ADMIN, "wrong password"),
        ("nobody@example.com", "whatever"),
        ("visitor@example.com", "visitor pass"),  # right password, not on the list
        ("pending@example.com", "pending pass"),  # on the list, address never confirmed
        ("", ""),
    ],
)
def test_every_refusal_looks_the_same(browser, email, password):
    r = sign_in(browser, email, password)
    assert r.status_code == 401
    assert GENERIC in r.text and 'role="alert"' in r.text
    assert "set-cookie" not in r.headers
    assert browser.get("/admin/api/templates").status_code == 401


def test_the_sign_in_page_keeps_the_address_and_escapes_it(browser):
    r = sign_in(browser, '"><script>alert(1)</script>@example.com', "x")
    assert "<script>alert(1)" not in r.text
    assert "&quot;&gt;&lt;script&gt;" in r.text


@pytest.mark.parametrize("failure", [500, 503, httpx.ConnectError("no route")])
def test_supabase_not_answering_is_a_different_message(browser, fake, failure):
    fake.fail_with = failure
    r = sign_in(browser)
    assert r.status_code == 503
    assert "Sign-in is not available right now" in r.text and GENERIC not in r.text
    assert "set-cookie" not in r.headers


def test_admin_page_redirects_to_sign_in_and_api_answers_401_json(browser):
    r = browser.get("/admin")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    r = browser.get("/admin/api/orders")
    assert r.status_code == 401 and r.json() == {"detail": "Sign in required"}
    assert "www-authenticate" not in r.headers
    page = browser.get("/admin/login")
    assert page.status_code == 200
    assert 'name="email"' in page.text and 'autocomplete="current-password"' in page.text
    assert "<!--message-->" not in page.text and "{{email}}" not in page.text


def test_sign_in_page_sends_a_signed_in_admin_on(browser):
    sign_in(browser)
    r = browser.get("/admin/login")
    assert r.status_code == 303 and r.headers["location"] == "/admin"


def test_a_tampered_cookie_is_refused(browser, auth_app):
    sign_in(browser)
    good = browser.cookies.get(COOKIE)
    body, signature = good.rsplit(".", 1)
    forged = auth_app.state.auth.issue("intruder@example.com").rsplit(".", 1)[0]
    for bad in (f"{forged}.{signature}", f"{body}.{'0' * 64}", body, "nonsense", f"{body}x.{signature}"):
        browser.cookies.clear()
        browser.cookies.set(COOKIE, bad, domain="jasmin.example", path="/admin")
        assert browser.get("/admin/api/templates").status_code == 401, bad
    browser.cookies.clear()
    browser.cookies.set(COOKIE, good, domain="jasmin.example", path="/admin")
    assert browser.get("/admin/api/templates").status_code == 200


def test_a_cookie_signed_with_another_secret_is_refused(browser, auth_settings):
    other = AdminAuth(Settings(**{**auth_settings.__dict__, "session_secret": "another secret"}))
    browser.cookies.set(COOKIE, other.issue(ADMIN), domain="jasmin.example", path="/admin")
    assert browser.get("/admin/api/templates").status_code == 401


def test_an_expired_cookie_is_refused(browser, auth_app):
    auth = auth_app.state.auth
    browser.cookies.set(COOKIE, auth.issue(ADMIN, now=time.time() - auth_module.SESSION_SECONDS - 5), domain="jasmin.example", path="/admin")
    assert browser.get("/admin/api/templates").status_code == 401
    assert browser.get("/admin").status_code == 303
    browser.cookies.clear()
    browser.cookies.set(COOKIE, auth.issue(ADMIN, now=time.time() - auth_module.SESSION_SECONDS + 60), domain="jasmin.example", path="/admin")
    assert browser.get("/admin/api/templates").status_code == 200


def test_an_address_taken_off_the_list_loses_its_session(browser, auth_settings):
    sign_in(browser)
    assert browser.get("/admin/api/templates").status_code == 200
    auth_settings.admin_emails.remove(ADMIN)
    assert browser.get("/admin/api/templates").status_code == 401


def test_sign_out_clears_the_cookie(browser):
    sign_in(browser)
    r = browser.post("/admin/logout")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert f'{COOKIE}=""' in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]
    assert browser.cookies.get(COOKIE) is None
    assert browser.get("/admin/api/templates").status_code == 401


def test_unsafe_requests_from_another_site_are_refused(browser):
    sign_in(browser)
    body = {"status": "shipped"}
    assert browser.post("/admin/api/orders/1/status", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser.delete("/admin/api/templates/nope", headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser.post("/admin/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser.post("/admin/login", data={"email": ADMIN, "password": PASSWORD}, headers={"Origin": "https://evil.example"}).status_code == 403
    # The app's own origin, the request's own host, and no Origin header at all are fine (404: no such template).
    assert browser.delete("/admin/api/templates/nope", headers={"Origin": ORIGIN}).status_code == 404
    assert browser.delete("/admin/api/templates/nope", headers={"Origin": "http://jasmin.example"}).status_code == 404
    assert browser.delete("/admin/api/templates/nope").status_code == 404
    # Reading is not affected.
    assert browser.get("/admin/api/templates", headers={"Origin": "https://evil.example"}).status_code == 200


def test_sign_in_attempts_are_throttled_per_client(browser, monkeypatch):
    for _ in range(auth_module.MAX_ATTEMPTS):
        assert sign_in(browser, password="wrong").status_code == 401
    r = sign_in(browser)
    assert r.status_code == 429 and "Too many sign-in attempts" in r.text
    assert "set-cookie" not in r.headers
    # Once the window has passed, the right password works again.
    real = time.monotonic
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: real() + auth_module.ATTEMPT_WINDOW + 1)
    assert sign_in(browser).status_code == 303


def test_throttle_counts_each_client_separately(auth_settings):
    auth = AdminAuth(auth_settings)
    assert [auth.throttled("a") for _ in range(auth_module.MAX_ATTEMPTS + 1)] == [False] * auth_module.MAX_ATTEMPTS + [True]
    assert auth.throttled("b") is False


def test_without_a_session_secret_one_is_made_up_and_a_warning_logged(auth_settings, fake, caplog):
    auth_settings.session_secret = ""
    with caplog.at_level("WARNING", logger="jasmin"):
        first = AdminAuth(auth_settings, fake.transport)
        second = AdminAuth(auth_settings, fake.transport)
    assert sum("SESSION_SECRET is not set" in r.message for r in caplog.records) == 2
    assert first.read(first.issue(ADMIN)) == ADMIN
    assert second.read(first.issue(ADMIN)) is None  # a restart ends the sessions


def test_settings_from_the_environment(monkeypatch):
    for name in ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY", "ADMIN_EMAILS", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert AdminAuth(load_settings()).enabled is False
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co/")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("ADMIN_EMAILS", " Jasmin@Example.com , second@example.com,, ")
    monkeypatch.setenv("SESSION_SECRET", " s ")
    s = load_settings()
    assert (s.supabase_publishable_key, s.admin_emails, s.session_secret) == ("anon", ["jasmin@example.com", "second@example.com"], "s")
    assert AdminAuth(s).enabled is True
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_x")
    assert load_settings().supabase_publishable_key == "sb_publishable_x"
    monkeypatch.setenv("ADMIN_EMAILS", " , ")
    assert AdminAuth(load_settings()).enabled is False


# -- Basic mode stays as it was ------------------------------------------------
def test_basic_mode_is_unchanged(client, admin, app):
    assert app.state.auth.enabled is False
    r = client.get("/admin")
    assert r.status_code == 401 and r.headers["www-authenticate"] == 'Basic realm="Jasmin admin"'
    assert client.get("/admin/api/templates").status_code == 401
    assert admin.get("/admin").status_code == 200
    assert admin.get("/admin/api/templates").status_code == 200
    assert admin.get("/admin/api/me").json() == {"email": None, "demoStore": True}
    assert client.post("/admin/login", data={"email": "a@b.c", "password": "x"}).status_code == 405
    assert client.post("/admin/logout").status_code in (404, 405)
    r = client.get("/admin/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    # A session cookie means nothing in basic mode.
    client.cookies.set(COOKIE, AdminAuth(app.state.settings).issue("jasmin@example.com"))
    assert client.get("/admin/api/templates").status_code == 401
