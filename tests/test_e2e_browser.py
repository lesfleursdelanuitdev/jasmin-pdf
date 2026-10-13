"""Full storefront -> editor -> cart -> webhook -> admin flow in a real browser.

Opt-in (needs Playwright and Chromium):

    pip install playwright && playwright install chromium
    JASMIN_E2E=1 pytest tests/test_e2e_browser.py

Set PLAYWRIGHT_CHROMIUM to use an existing Chromium binary.
"""
from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from .conftest import PHOTOS

pytestmark = pytest.mark.skipif(os.environ.get("JASMIN_E2E") != "1", reason="set JASMIN_E2E=1 to run")


@pytest.fixture
def server(app, template):
    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


def test_customer_journey(server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM") or None)
        ctx = browser.new_context(http_credentials={"username": "admin", "password": "jasmin"})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(f"{server}/demo-store/products/wedding-invitation")
        page.wait_for_selector(".jasmin-btn")
        assert not page.locator('button[name="add"]').is_visible()
        page.click(".jasmin-btn")

        frame = page.frame_locator(".jasmin-frame")
        frame.locator("#paper image").first.wait_for()
        with page.expect_file_chooser() as chooser:
            frame.locator('.field[data-field="couple-photo"] .photo-thumb').click()
        chooser.value.set_files(str(PHOTOS / "dusk-portrait.jpg"))
        frame.locator('.field[data-field="couple-photo"] .photo-thumb.has-photo').wait_for(timeout=15000)
        frame.locator("#f-couple-names").fill("Sofia & Mateo")
        rendered = frame.locator('#paper [data-el="couple-names"] text').text_content()
        assert rendered == "Sofia & Mateo"

        frame.locator("#review-btn").click()
        frame.locator("#confirm-check").check(timeout=30000)
        frame.locator("text=Approve & add to cart").click()
        page.wait_for_url("**/demo-store/cart", timeout=30000)
        assert "Sofia & Mateo" in page.locator(".line-info").inner_text()

        page.click("text=Place order")
        page.wait_for_url("**/demo-store/orders/**", timeout=30000)
        page.click("text=See it in the admin")
        page.wait_for_selector("text=Download print PDF")
        assert errors == []
        browser.close()
