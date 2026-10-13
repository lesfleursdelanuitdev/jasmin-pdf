from __future__ import annotations

import sys
from pathlib import Path

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


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", app_url="https://jasmin.example", shopify_webhook_secret="s3cret")


@pytest.fixture
def app(settings):
    return create_app(settings)


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
