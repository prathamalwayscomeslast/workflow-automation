"""Worker process: N executor threads + 1 outbox dispatcher thread.

Workers are stateless. Run as many processes as you like (`python -m app.engine.worker`),
on as many machines as you like; SKIP LOCKED + leases keep them from stepping on each other.
Recovery needs no special startup phase: expired leases are simply "due work" in the claim
query, so a restarted (or any other) worker picks those runs up continuously."""
from __future__ import annotations

import logging
import signal
import threading
import time

from ..config import get_settings, new_worker_id
from ..db import init_db
from . import outbox
from .executor import claim_next, process_run

log = logging.getLogger("engine.worker")


class Worker:
    def __init__(self, concurrency: int | None = None) -> None:
        self.settings = get_settings()
        self.concurrency = concurrency or self.settings.worker_concurrency
        self.worker_id = new_worker_id()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _executor_loop(self, n: int) -> None:
        wid = f"{self.worker_id}/t{n}"
        while not self._stop.is_set():
            try:
                run_id = claim_next(wid)
                if run_id is None:
                    self._stop.wait(self.settings.poll_interval_seconds)
                    continue
                outcome = process_run(run_id, wid)
                log.debug("run %s -> %s", run_id, outcome)
            except Exception:  # DB blip (e.g. Neon waking up): back off, keep going
                log.exception("executor loop error")
                self._stop.wait(2.0)

    def _dispatcher_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if outbox.deliver_pending(limit=100) == 0:
                    self._stop.wait(self.settings.poll_interval_seconds)
            except Exception:
                log.exception("dispatcher loop error")
                self._stop.wait(2.0)

    def start(self) -> None:
        log.info("worker %s starting with %s executor threads", self.worker_id, self.concurrency)
        for i in range(self.concurrency):
            t = threading.Thread(target=self._executor_loop, args=(i,), name=f"exec-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        d = threading.Thread(target=self._dispatcher_loop, name="dispatcher", daemon=True)
        d.start()
        self._threads.append(d)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    w = Worker()
    w.start()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print(f"WORKER_READY {w.worker_id}", flush=True)
    while not stop.is_set():
        time.sleep(0.5)
    w.stop()


if __name__ == "__main__":
    main()
