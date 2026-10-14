"""Work that is done after the request that asked for it: a ``jobs`` table and one worker.

    jobs.enqueue("print", "print:1001", {"order_id": "1001"})

A job is a row, so it is not lost when the app stops: one that was running then is
picked up again at the next start. There is one job per ``key``; asking again for
work that is queued or running changes nothing, so a webhook delivered twice does
not render twice. The worker takes the jobs one at a time, which also keeps the
rendering from using more memory than one render needs. A job that fails is tried
again a few times, then kept with its error.

With ``inline`` (settings made in code: tests, scripts) a job runs at once in the
caller's thread and there is no worker.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from .db import dumps, loads, now

log = logging.getLogger("jasmin")

MAX_ATTEMPTS = 3
RETRY_PAUSE = 30.0  # seconds before a failed job is tried again
IDLE_POLL = 60.0  # the worker is woken when a job arrives; this is only its safety net


class Jobs:
    def __init__(self, db, handlers: dict[str, Callable[[dict], None]], inline: bool = True):
        self.db = db
        self.handlers = handlers
        self.inline = inline
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self.retry_pause = RETRY_PAUSE

    # -- asking for work -------------------------------------------------
    def enqueue(self, kind: str, key: str, payload: dict | None = None) -> None:
        if kind not in self.handlers:
            raise ValueError(f"no handler for job kind {kind!r}")
        ts = now()
        # A finished or failed job with this key is queued afresh; one that is waiting or running stays as it is.
        self.db.execute(
            "INSERT INTO jobs (kind, key, payload, status, attempts, created_at, updated_at) VALUES (?,?,?, 'queued', 0, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET status='queued', attempts=0, error=NULL, payload=excluded.payload, updated_at=excluded.updated_at"
            " WHERE jobs.status IN ('done', 'error')",
            (kind, key, dumps(payload or {}), ts, ts),
        )
        if self.inline:
            self.run_pending()
        else:
            self._wake.set()

    def status(self, key: str) -> dict | None:
        return self.db.one("SELECT kind, key, status, attempts, error, updated_at FROM jobs WHERE key=?", (key,))

    # -- doing it --------------------------------------------------------
    def _claim(self, skip: set[int] = frozenset()) -> dict | None:
        while True:
            job = next((j for j in self.db.all("SELECT * FROM jobs WHERE status='queued' ORDER BY id") if j["id"] not in skip), None)
            if job is None:
                return None
            # Taken only if nobody else took it in between (a second process, say).
            if self.db.execute("UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=? AND status='queued'", (now(), job["id"])):
                job["attempts"] += 1
                return job

    def _run(self, job: dict) -> bool:
        try:
            self.handlers[job["kind"]](loads(job["payload"], {}))
        except Exception as exc:
            final = job["attempts"] >= MAX_ATTEMPTS
            log.exception("job %s failed (attempt %s of %s)", job["key"], job["attempts"], MAX_ATTEMPTS)
            self.db.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                ("error" if final else "queued", str(exc)[:1000] or exc.__class__.__name__, now(), job["id"]),
            )
            return False
        self.db.execute("UPDATE jobs SET status='done', error=NULL, updated_at=? WHERE id=?", (now(), job["id"]))
        return True

    def run_pending(self) -> int:
        """Run every job that is waiting, in this thread; returns how many succeeded. A job that fails is not retried here."""
        done, failed = 0, set()
        while True:
            job = self._claim(skip=failed)  # a job that just failed waits for the next round
            if job is None:
                return done
            if self._run(job):
                done += 1
            else:
                failed.add(job["id"])

    # -- the worker ------------------------------------------------------
    def recover(self) -> int:
        """Jobs that were running when the app last stopped wait again. Called once, at start."""
        return self.db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='running'", (now(),))

    def start(self) -> None:
        if self.inline or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._work, name="jasmin-jobs", daemon=True)
        self._thread.start()

    def _work(self) -> None:
        while not self._halt.is_set():
            self._wake.clear()
            try:
                self.run_pending()
                waiting = self.db.one("SELECT COUNT(*) AS n FROM jobs WHERE status='queued'")["n"]
            except Exception:  # the database is away: try again later, never die
                log.exception("the job worker could not reach the database")
                waiting = 1
            # Something is still queued only when it just failed: give it a pause before the next try.
            self._wake.wait(self.retry_pause if waiting else IDLE_POLL)

    def stop(self, timeout: float = 5.0) -> None:
        self._halt.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
