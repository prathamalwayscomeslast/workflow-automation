"""Claiming and executing runs.

Invariants
- A run is only advanced by the worker holding its lease (lease_owner = me).
- Each step's effects are committed in ONE transaction: run_steps row + context update +
  current_step advance (+ outbox insert for messages). The UPDATE is fenced on
  (lease_owner, status, step_count), so a worker that lost its lease can never commit.
- Nothing about a run lives in memory between steps. Kill the process at any instant and
  the database still describes exactly what has happened; the worst case is that the
  in-flight step runs again (sends stay safe via the outbox idempotency key)."""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import func, text, update

from ..config import get_settings
from ..db import session_scope
from ..models import Run, RunStatus, RunStep, Workflow
from ..schemas import END, Step
from . import outbox
from .steps import STEP_HANDLERS, StepContext, StepResult

log = logging.getLogger("engine.executor")
_step_adapter: TypeAdapter[Any] = TypeAdapter(Step)

# Due work = new runs, finished waits, and runs whose worker died (lease expired).
CLAIM_SQL = text(
    """
    UPDATE runs
       SET status = 'running',
           lease_owner = :me,
           lease_expires_at = now() + make_interval(secs => CAST(:lease AS double precision)),
           claim_count = claim_count + 1,
           updated_at = now()
     WHERE id = (
            SELECT id FROM runs
             WHERE (status IN ('pending', 'waiting') AND wake_at <= now())
                OR (status = 'running' AND lease_expires_at < now())
             ORDER BY wake_at NULLS FIRST, id
             LIMIT 1
             FOR UPDATE SKIP LOCKED
           )
    RETURNING id, claim_count
    """
)


class LeaseLost(Exception):
    pass


def claim_next(worker_id: str) -> int | None:
    lease = get_settings().lease_seconds
    with session_scope() as s:
        row = s.execute(CLAIM_SQL, {"me": worker_id, "lease": lease}).first()
    if row is None:
        return None
    if row.claim_count > 1:
        log.info("claimed run %s (claim #%s)", row.id, row.claim_count)
    return int(row.id)


@dataclass
class _Snapshot:
    steps: list[dict[str, Any]]
    index: int | None
    seq: int
    context: dict[str, Any]


def _load(run_id: int, worker_id: str) -> _Snapshot | None:
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None or run.status != RunStatus.RUNNING or run.lease_owner != worker_id:
            return None
        wf = s.get(Workflow, run.workflow_id)
        steps = wf.steps if wf else []
        return _Snapshot(steps, run.current_step, run.step_count, copy.deepcopy(run.context or {}))


def _resolve_next(steps: list[dict[str, Any]], index: int, step: Any, result: StepResult) -> int | None:
    target = result.next if result.next is not None else step.next
    if target == END:
        return None
    if target is not None:
        for i, s in enumerate(steps):
            if s.get("id") == target:
                return i
        raise ValueError(f"unknown step target {target!r}")
    return index + 1 if index + 1 < len(steps) else None


def _fenced_update(s: Any, run_id: int, worker_id: str, seq: int, values: dict[str, Any]) -> None:
    res = s.execute(
        update(Run)
        .where(
            Run.id == run_id,
            Run.lease_owner == worker_id,
            Run.status == RunStatus.RUNNING,
            Run.step_count == seq,
        )
        .values(**values)
    )
    if res.rowcount != 1:
        raise LeaseLost(f"run {run_id}: lease lost before commit; discarding step result")


def _finish(run_id: int, worker_id: str, seq: int) -> None:
    with session_scope() as s:
        _fenced_update(s, run_id, worker_id, seq, {
            "status": RunStatus.COMPLETED, "current_step": None, "finished_at": func.now(),
            "updated_at": func.now(), "lease_owner": None, "lease_expires_at": None,
        })


def process_run(run_id: int, worker_id: str) -> str:
    """Execute steps of a claimed run until it waits, finishes, fails, or hits the per-claim
    step budget (then it is released as pending so other runs get a turn)."""
    settings = get_settings()
    for _ in range(settings.max_steps_per_claim):
        snap = _load(run_id, worker_id)
        if snap is None:
            return "lost"
        if snap.index is None or snap.index >= len(snap.steps):
            _finish(run_id, worker_id, snap.seq)
            return RunStatus.COMPLETED

        raw = snap.steps[snap.index]
        started = datetime.now(timezone.utc)
        step: Any = None
        try:
            step = _step_adapter.validate_python(raw)
            result = STEP_HANDLERS[step.type](step, StepContext(run_id, snap.seq, snap.context))
            next_index = _resolve_next(snap.steps, snap.index, step, result)
        except Exception as e:  # step failed -> run failed (retry policies are a next step)
            log.warning("run %s step %s failed: %s", run_id, raw.get("id"), e)
            with session_scope() as s:
                _fenced_update(s, run_id, worker_id, snap.seq, {
                    "status": RunStatus.FAILED, "error": f"{raw.get('id')}: {e}"[:2000],
                    "step_count": snap.seq + 1, "finished_at": func.now(), "updated_at": func.now(),
                    "lease_owner": None, "lease_expires_at": None,
                })
                s.add(RunStep(run_id=run_id, seq=snap.seq, step_index=snap.index,
                              step_id=str(raw.get("id")), step_type=str(raw.get("type")),
                              status="failed", error=str(e)[:2000], worker_id=worker_id,
                              started_at=started))
            return RunStatus.FAILED

        ctx = snap.context
        ctx.setdefault("steps", {})[step.id] = result.output
        if result.vars:
            ctx.setdefault("vars", {}).update(result.vars)

        values: dict[str, Any] = {
            "context": ctx,
            "step_count": snap.seq + 1,
            "updated_at": func.now(),
        }
        outcome = "continue"
        if next_index is None:
            values.update(status=RunStatus.COMPLETED, current_step=None, finished_at=func.now(),
                          lease_owner=None, lease_expires_at=None)
            outcome = RunStatus.COMPLETED
        elif result.wait_seconds is not None and result.wait_seconds > 0:
            # Durable wait: the run becomes a row with a wake-up time; the worker moves on.
            values.update(status=RunStatus.WAITING, current_step=next_index,
                          wake_at=func.now() + timedelta(seconds=result.wait_seconds),
                          lease_owner=None, lease_expires_at=None)
            outcome = RunStatus.WAITING
        else:
            values.update(current_step=next_index,
                          lease_expires_at=func.now() + timedelta(seconds=settings.lease_seconds))

        try:
            with session_scope() as s:
                _fenced_update(s, run_id, worker_id, snap.seq, values)
                s.add(RunStep(run_id=run_id, seq=snap.seq, step_index=snap.index, step_id=step.id,
                              step_type=step.type, status="completed", output=result.output,
                              worker_id=worker_id, started_at=started))
                if result.outbox:
                    outbox.enqueue(s, run_id, step.id, result.outbox)
        except LeaseLost as e:
            log.warning("%s", e)
            return "lost"

        if result.outbox:  # fast path; the dispatcher loop picks up anything left behind
            try:
                outbox.deliver_pending(keys=[result.outbox["idempotency_key"]])
            except Exception as e:
                log.warning("inline delivery failed (dispatcher will retry): %s", e)

        if outcome != "continue":
            return outcome

    # Step budget exhausted: hand the run back so one long workflow can't starve others.
    with session_scope() as s:
        s.execute(
            update(Run)
            .where(Run.id == run_id, Run.lease_owner == worker_id, Run.status == RunStatus.RUNNING)
            .values(status=RunStatus.PENDING, wake_at=func.now(), lease_owner=None, lease_expires_at=None)
        )
    return RunStatus.PENDING
