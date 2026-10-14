from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.fonts import FontLibrary  # noqa: E402
from app.main import create_app  # noqa: E402
from scripts.seed import seed  # noqa: E402

SAMPLE_PDF = ROOT / "samples" / "wedding-invitation.pdf"
PHOTOS = ROOT / "samples" / "photos"


@pytest.fixture(scope="session")
def fonts() -> FontLibrary:
    return FontLibrary(ROOT / "fonts", ROOT / "data-test-nonexistent")


# Set JASMIN_TEST_DATABASE_URL to run the suite on Postgres instead of SQLite.
# Every test then gets a schema of its own, dropped afterwards.
TEST_DATABASE_URL = os.environ.get("JASMIN_TEST_DATABASE_URL", "")


@pytest.fixture
def database_url():
    if not TEST_DATABASE_URL:
        yield ""
        return
    import psycopg

    schema = f"t_{secrets.token_hex(8)}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    parts = urlsplit(TEST_DATABASE_URL)
    query = "&".join(filter(None, [parts.query, urlencode({"options": f"-csearch_path={schema}"})]))
    yield urlunsplit(parts._replace(query=query))
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def settings(tmp_path, database_url) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        app_url="https://jasmin.example",
        shopify_webhook_secret="s3cret",
        database_url=database_url,
    )


@pytest.fixture
def app(settings):
    app = create_app(settings)
    yield app
    app.state.services.db.close()


@pytest.fixture
def svc(app):
    return app.state.services


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin(app) -> TestClient:
    c = TestClient(app)
    c.auth = ("admin", "jasmin")
    return c


@pytest.fixture
def template(svc) -> dict:
    return seed(svc)
