from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..engine.triggers import dispatch_event
from ..models import Outbox, Run, RunStep, SinkMessage, Workflow
from ..schemas import EventIn, EventOut, RunDetail, RunOut, RunStepOut

router = APIRouter(tags=["runs"])


@router.post("/events", response_model=EventOut, status_code=202)
def post_event(ev: EventIn, db: Session = Depends(get_db)) -> EventOut:
    """Accept an event and enqueue runs. Nothing executes in the request."""
    matched, run_ids, skipped = dispatch_event(db, ev.event, ev.payload, ev.idempotency_key)
    return EventOut(matched_workflows=matched, run_ids=run_ids, skipped=skipped)


@router.get("/runs", response_model=list[RunOut])
def list_runs(workflow_id: int | None = None, status: str | None = None,
              limit: int = Query(50, le=500), db: Session = Depends(get_db)) -> list[Run]:
    q = select(Run).order_by(Run.id.desc()).limit(limit)
    if workflow_id is not None:
        q = q.where(Run.workflow_id == workflow_id)
    if status:
        q = q.where(Run.status == status)
    return list(db.scalars(q))


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: int, db: Session = Depends(get_db)) -> RunDetail:
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    steps = db.scalars(select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.seq)).all()
    wf = db.get(Workflow, run.workflow_id)
    detail = RunDetail.model_validate(run)
    detail.steps = [RunStepOut.model_validate(s) for s in steps]
    detail.workflow_steps = wf.steps if wf else []
    return detail


@router.get("/runs/{run_id}/messages")
def run_messages(run_id: int, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(Outbox).where(Outbox.run_id == run_id).order_by(Outbox.id)).all()
    keys = [r.idempotency_key for r in rows]
    sink = {m.idempotency_key: m for m in db.scalars(
        select(SinkMessage).where(SinkMessage.idempotency_key.in_(keys)))} if keys else {}
    return [{
        "idempotency_key": r.idempotency_key, "step_id": r.step_id, "channel": r.channel,
        "to": r.recipient, "payload": r.payload, "outbox_status": r.status, "attempts": r.attempts,
        "delivered": r.idempotency_key in sink,
        "provider_attempts": sink[r.idempotency_key].attempts if r.idempotency_key in sink else 0,
        "sent_at": r.sent_at,
    } for r in rows]


@router.get("/stats")
def stats(db: Session = Depends(get_db)) -> dict[str, Any]:
    runs = dict(db.execute(select(Run.status, func.count()).group_by(Run.status)).all())
    outbox = dict(db.execute(select(Outbox.status, func.count()).group_by(Outbox.status)).all())
    return {
        "workflows": db.scalar(select(func.count()).select_from(Workflow)),
        "runs": runs,
        "outbox": outbox,
        "sink_messages": db.scalar(select(func.count()).select_from(SinkMessage)),
        "sink_duplicate_attempts_absorbed": db.scalar(
            select(func.coalesce(func.sum(SinkMessage.attempts - 1), 0))),
    }
