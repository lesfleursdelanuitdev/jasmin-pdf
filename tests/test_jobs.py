"""Jobs: work that survives a restart, is not done twice, and is tried again when it fails."""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.jobs import MAX_ATTEMPTS, Jobs
from app.shopify import sign

from .test_api import approved_design, order_payload


@pytest.fixture
def ran():
    return []


def make(svc, ran, inline=True, fail=None):
    def handler(payload):
        ran.append(payload["n"])
        if fail and fail(payload):
            raise RuntimeError("it went wrong")

    return Jobs(svc.db, {"work": handler}, inline=inline)


def test_a_job_runs_and_is_marked_done(svc, ran):
    jobs = make(svc, ran)
    jobs.enqueue("work", "work:1", {"n": 1})
    assert ran == [1] and jobs.status("work:1")["status"] == "done"
    with pytest.raises(ValueError):
        jobs.enqueue("unknown", "x:1")


def test_the_same_work_is_not_queued_twice_but_can_be_asked_for_again_once_done(svc, ran):
    jobs = make(svc, ran, inline=False)  # nothing runs until asked
    jobs.enqueue("work", "work:1", {"n": 1})
    jobs.enqueue("work", "work:1", {"n": 99})  # a webhook delivered twice
    assert svc.db.one("SELECT COUNT(*) AS n FROM jobs")["n"] == 1
    assert jobs.run_pending() == 1 and ran == [1]
    jobs.enqueue("work", "work:1", {"n": 2})  # done: queued afresh, with the new payload
    assert jobs.run_pending() == 1 and ran == [1, 2] and jobs.status("work:1")["attempts"] == 1


def test_a_failing_job_is_tried_again_and_then_kept_with_its_error(svc, ran):
    jobs = make(svc, ran, inline=False, fail=lambda p: True)
    jobs.enqueue("work", "work:1", {"n": 1})
    for attempt in range(1, MAX_ATTEMPTS + 1):
        assert jobs.run_pending() == 0
        st = jobs.status("work:1")
        assert st["attempts"] == attempt and st["status"] == ("error" if attempt == MAX_ATTEMPTS else "queued")
    assert jobs.status("work:1")["error"] == "it went wrong" and len(ran) == MAX_ATTEMPTS
    assert jobs.run_pending() == 0 and len(ran) == MAX_ATTEMPTS  # given up: not tried a fourth time


def test_one_failing_job_does_not_hold_up_the_others(svc, ran):
    jobs = make(svc, ran, inline=False, fail=lambda p: p["n"] == 1)
    for n in (1, 2, 3):
        jobs.enqueue("work", f"work:{n}", {"n": n})
    assert jobs.run_pending() == 2 and ran == [1, 2, 3]
    assert [jobs.status(f"work:{n}")["status"] for n in (1, 2, 3)] == ["queued", "done", "done"]


def test_a_job_that_was_running_when_the_app_stopped_runs_again(svc, ran):
    jobs = make(svc, ran, inline=False)
    jobs.enqueue("work", "work:1", {"n": 1})
    svc.db.execute("UPDATE jobs SET status='running', attempts=1")  # the app stopped in the middle of it
    assert jobs.run_pending() == 0
    assert jobs.recover() == 1 and jobs.run_pending() == 1 and ran == [1]


def test_the_worker_does_the_jobs_one_at_a_time_and_stops_when_told(svc):
    running, most, order = [0], [0], []
    lock = threading.Lock()

    def handler(payload):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.05)
        order.append(payload["n"])
        with lock:
            running[0] -= 1

    jobs = Jobs(svc.db, {"work": handler}, inline=False)
    jobs.start()
    try:
        for n in range(5):
            jobs.enqueue("work", f"work:{n}", {"n": n})
        for _ in range(100):
            if len(order) == 5:
                break
            time.sleep(0.05)
        assert order == [0, 1, 2, 3, 4] and most[0] == 1
    finally:
        jobs.stop()
    assert not jobs._thread.is_alive()


def test_the_worker_tries_a_failed_job_again_after_a_pause(svc, ran):
    jobs = make(svc, ran, inline=False, fail=lambda p: len(ran) < 2)  # the first try fails
    jobs.retry_pause = 0.1
    jobs.start()
    try:
        jobs.enqueue("work", "work:1", {"n": 1})
        for _ in range(100):
            if (jobs.status("work:1") or {}).get("status") == "done":
                break
            time.sleep(0.05)
        assert jobs.status("work:1")["status"] == "done" and len(ran) == 2
    finally:
        jobs.stop()


# -- print files as jobs ---------------------------------------------------------
def webhook(client, payload):
    body = json.dumps(payload).encode()
    return client.post("/webhooks/shopify/orders-create", content=body, headers={"X-Shopify-Hmac-Sha256": sign(body, "s3cret"), "Content-Type": "application/json"})


def test_an_order_makes_one_print_job_however_often_the_webhook_arrives(app, client, svc, template):
    design = approved_design(client)
    payload = order_payload(design["id"])
    for _ in range(3):
        assert webhook(client, payload).status_code == 200
    key = f"print:{payload['id']}"
    assert svc.db.one("SELECT COUNT(*) AS n FROM jobs WHERE key=?", (key,))["n"] == 1
    assert app.state.jobs.status(key)["status"] == "done"
    assert svc.db.one("SELECT status FROM order_items")["status"] == "ready"


def test_a_print_file_that_could_not_be_made_is_tried_again_by_its_job(app, client, svc, template, monkeypatch):
    design = approved_design(client)
    payload = order_payload(design["id"])
    real = svc._render
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("storage hiccup")
        return real(*a, **k)

    monkeypatch.setattr(svc, "_render", flaky)
    assert webhook(client, payload).status_code == 200  # the webhook is answered even though the first try failed
    key = f"print:{payload['id']}"
    assert app.state.jobs.status(key)["status"] == "queued" and "storage hiccup" in app.state.jobs.status(key)["error"]
    assert svc.db.one("SELECT status FROM order_items")["status"] == "error"
    assert app.state.jobs.run_pending() == 1
    assert svc.db.one("SELECT status FROM order_items")["status"] == "ready" and app.state.jobs.status(key)["status"] == "done"


# -- approving the proof that was looked at ---------------------------------------
def test_approving_right_after_the_proof_does_not_render_again(client, svc, template, monkeypatch):
    from .test_api import upload

    photo = upload(client)
    d = client.post("/api/designs", json={"templateId": "wedding-invitation", "values": {"couple-photo": {"upload": photo["id"]}, "couple-names": {"text": "Sofia & Mateo"}}}).json()
    renders = []
    real = svc._render
    monkeypatch.setattr(svc, "_render", lambda *a, **k: renders.append(1) or real(*a, **k))

    assert client.post(f"/api/designs/{d['id']}/proof").json()["errors"] == []
    assert client.post(f"/api/designs/{d['id']}/approve").json()["status"] == "approved"
    assert len(renders) == 1

    # Changed after the proof: approving renders the proof of what is actually approved.
    d2 = client.post("/api/designs", json={"templateId": "wedding-invitation", "values": {"couple-photo": {"upload": photo["id"]}, "couple-names": {"text": "A & B"}}}).json()
    client.post(f"/api/designs/{d2['id']}/proof")
    client.put(f"/api/designs/{d2['id']}", json={"values": {"couple-photo": {"upload": photo["id"]}, "couple-names": {"text": "C & D"}}})
    assert client.post(f"/api/designs/{d2['id']}/approve").json()["status"] == "approved"
    assert len(renders) == 3 and "C & D" in svc.get_design(d2["id"])["summary"]

    # The proof file is gone (a lost working copy that the store does not have either): rendered again.
    d3 = client.post("/api/designs", json={"templateId": "wedding-invitation", "values": {"couple-photo": {"upload": photo["id"]}, "couple-names": {"text": "E & F"}}}).json()
    client.post(f"/api/designs/{d3['id']}/proof")
    svc.storage.remove([f"designs/{d3['id']}/proof.pdf"])
    assert client.post(f"/api/designs/{d3['id']}/approve").json()["status"] == "approved" and len(renders) == 5
