"""Crash-recovery tests against a real Postgres (DATABASE_URL in backend/.env).

test_sigkill_mid_wait_resumes_without_duplicates
    Starts a worker process, triggers N runs, SIGKILLs the worker while runs are mid-wait
    (and some mid-step), restarts it, and asserts every run completes with each message
    delivered exactly once.

test_crash_between_delivery_and_mark_sent
    Forces the nastiest window: the provider accepted the message but the process dies
    before the outbox row is marked sent. After restart the message is re-attempted, and the
    provider's idempotency key dedupes it: attempts == 2, delivered messages == 1.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402

if not get_settings().database_url:
    pytest.skip("DATABASE_URL not configured (backend/.env)", allow_module_level=True)

from app.db import init_db, session_scope  # noqa: E402
from app.engine.triggers import dispatch_event  # noqa: E402
from app.models import Outbox, Run, RunStep, SinkMessage, Workflow  # noqa: E402
from app.schemas import WorkflowSpec  # noqa: E402

N_RUNS = 12
WAIT_SECONDS = 6
TIMEOUT = 180  # Neon cold starts + cross-region latency


@pytest.fixture(scope="module", autouse=True)
def _db() -> None:
    init_db()


def _spec(event: str) -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "name": f"recovery-test {event}",
        "trigger": {"event": event, "conditions": {"field": "amount", "op": "gt", "value": 500}},
        "steps": [
            {"id": "score", "type": "task", "task": "score_order", "params": {"amount": "{{trigger.amount}}"}},
            {"id": "work", "type": "task", "task": "simulate_work", "params": {"ms": 300}},
            {"id": "thanks", "type": "send_message", "channel": "email", "to": "{{trigger.email}}",
             "body": "Thanks for order {{trigger.order_id}}"},
            {"id": "pause", "type": "wait", "duration": f"{WAIT_SECONDS} seconds"},
            {"id": "is_gold", "type": "branch",
             "condition": {"field": "steps.score.tier", "op": "eq", "value": "gold"},
             "then": "vip", "else": "followup"},
            {"id": "vip", "type": "send_message", "to": "{{trigger.email}}",
             "body": "VIP follow-up for {{trigger.order_id}}", "next": "end"},
            {"id": "followup", "type": "send_message", "to": "{{trigger.email}}",
             "body": "Follow-up for {{trigger.order_id}}"},
        ],
    })


def _create(event: str, n: int) -> tuple[int, list[int]]:
    spec = _spec(event)
    with session_scope() as s:
        wf = Workflow(name=spec.name, trigger=spec.dump_trigger(), steps=spec.dump_steps(), source="json")
        s.add(wf)
        s.flush()
        wf_id = wf.id
    run_ids: list[int] = []
    with session_scope() as s:
        for i in range(n):
            amount = 600 + i * 100  # mix of silver and gold
            _, ids, _ = dispatch_event(s, event, {"order_id": f"o-{i}", "amount": amount,
                                                  "email": f"c{i}@example.com"})
            run_ids += ids
        # below threshold -> must not start a run
        _, ids, skipped = dispatch_event(s, event, {"order_id": "small", "amount": 100})
        assert ids == [] and skipped
    assert len(run_ids) == n
    return wf_id, run_ids


def _start_worker(tmp_path: Path, name: str, **env_overrides: Any) -> tuple[subprocess.Popen, Path]:
    log = tmp_path / f"{name}.log"
    env = {**os.environ, "RUN_WORKER": "false", "WORKER_CONCURRENCY": "6",
           "LEASE_SECONDS": "4", "POLL_INTERVAL_SECONDS": "0.3", "WAIT_TIME_SCALE": "1",
           "PYTHONUNBUFFERED": "1"}
    env.update({k.upper(): str(v) for k, v in env_overrides.items()})
    proc = subprocess.Popen([sys.executable, "-m", "app.engine.worker"], cwd=BACKEND, env=env,
                            stdout=log.open("w"), stderr=subprocess.STDOUT)
    deadline = time.time() + 60
    while time.time() < deadline:
        if "WORKER_READY" in log.read_text(errors="ignore"):
            return proc, log
        if proc.poll() is not None:
            raise AssertionError(f"worker exited early:\n{log.read_text()}")
        time.sleep(0.2)
    proc.kill()
    raise AssertionError(f"worker did not start:\n{log.read_text()}")


def _sigkill(proc: subprocess.Popen) -> None:
    if hasattr(signal, "SIGKILL"):
        proc.send_signal(signal.SIGKILL)
    else:  # Windows: TerminateProcess, equally abrupt (no cleanup, no atexit)
        proc.kill()
    proc.wait(10)


def _status_counts(run_ids: list[int]) -> dict[str, int]:
    with session_scope() as s:
        rows = s.execute(select(Run.status, func.count()).where(Run.id.in_(run_ids))
                         .group_by(Run.status)).all()
    return {st: c for st, c in rows}


def _wait_for(pred, timeout: float, what: str, log: Path | None = None) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.5)
    tail = log.read_text(errors="ignore")[-3000:] if log else ""
    raise AssertionError(f"timed out waiting for {what}\n{tail}")


def _assert_exactly_once(run_ids: list[int]) -> None:
    with session_scope() as s:
        runs = s.scalars(select(Run).where(Run.id.in_(run_ids))).all()
        assert all(r.status == "completed" for r in runs), [(r.id, r.status, r.error) for r in runs]

        outbox_rows = s.scalars(select(Outbox).where(Outbox.run_id.in_(run_ids))).all()
        assert len(outbox_rows) == 2 * len(run_ids), "each run enqueues exactly 2 messages"
        assert all(o.status == "sent" for o in outbox_rows)

        keys = [o.idempotency_key for o in outbox_rows]
        sink = s.scalars(select(SinkMessage).where(SinkMessage.idempotency_key.in_(keys))).all()
        assert len(sink) == len(keys), "every message reached the provider exactly once"

        per_run: dict[int, int] = {}
        for o in outbox_rows:
            per_run[o.run_id] = per_run.get(o.run_id, 0) + 1
        assert set(per_run.values()) == {2}

        # Audit trail is gap-free and has no duplicate step commits.
        for r in runs:
            seqs = s.scalars(select(RunStep.seq).where(RunStep.run_id == r.id).order_by(RunStep.seq)).all()
            assert seqs == list(range(r.step_count)), (r.id, seqs)
            assert r.step_count == 6  # score, work, thanks, pause, is_gold, vip|followup


def _cleanup(wf_id: int) -> None:
    with session_scope() as s:
        s.execute(delete(Workflow).where(Workflow.id == wf_id))


def test_sigkill_mid_wait_resumes_without_duplicates(tmp_path: Path) -> None:
    event = f"test.order.{uuid.uuid4().hex[:8]}"
    wf_id, run_ids = _create(event, N_RUNS)

    proc, log1 = _start_worker(tmp_path, "worker1")
    # Kill once most runs are parked in their wait (first message already sent).
    _wait_for(lambda: _status_counts(run_ids).get("waiting", 0) >= N_RUNS // 2,
              TIMEOUT, "runs to reach the wait step", log1)
    _sigkill(proc)

    before = _status_counts(run_ids)
    print("status at SIGKILL:", before)
    assert before.get("completed", 0) < N_RUNS, "kill must happen mid-flight"

    time.sleep(1)
    proc2, log2 = _start_worker(tmp_path, "worker2")
    try:
        _wait_for(lambda: _status_counts(run_ids).get("completed", 0) == N_RUNS,
                  TIMEOUT, "all runs to complete after restart", log2)
    finally:
        proc2.terminate()
        proc2.wait(15)

    _assert_exactly_once(run_ids)
    with session_scope() as s:
        resumed = s.scalar(select(func.count()).where(Run.id.in_(run_ids), Run.claim_count > 1))
    assert resumed >= N_RUNS // 2, "runs were resumed by the second worker"
    _cleanup(wf_id)


def test_crash_between_delivery_and_mark_sent(tmp_path: Path) -> None:
    event = f"test.order.{uuid.uuid4().hex[:8]}"
    wf_id, run_ids = _create(event, 4)

    # Worker hard-exits right after the provider accepts delivery #2, before marking it sent.
    proc, log1 = _start_worker(tmp_path, "chaos", chaos_exit_after_deliveries=2)
    assert proc.wait(TIMEOUT) == 17, log1.read_text()

    with session_scope() as s:
        pending = s.scalars(select(Outbox).where(Outbox.run_id.in_(run_ids), Outbox.status == "pending")).all()
        pending_keys = [o.idempotency_key for o in pending]
        delivered_but_unmarked = s.scalars(
            select(SinkMessage.idempotency_key).where(SinkMessage.idempotency_key.in_(pending_keys))).all()
    assert delivered_but_unmarked, "crash window reproduced: provider has it, outbox says pending"

    proc2, log2 = _start_worker(tmp_path, "after-chaos")
    try:
        _wait_for(lambda: _status_counts(run_ids).get("completed", 0) == len(run_ids),
                  TIMEOUT, "all runs to complete", log2)
    finally:
        proc2.terminate()
        proc2.wait(15)

    _assert_exactly_once(run_ids)
    with session_scope() as s:
        attempts = s.scalars(select(SinkMessage.attempts)
                             .where(SinkMessage.idempotency_key.in_(delivered_but_unmarked))).all()
    assert all(a == 2 for a in attempts), "re-attempted once, deduped by the provider"
    _cleanup(wf_id)
