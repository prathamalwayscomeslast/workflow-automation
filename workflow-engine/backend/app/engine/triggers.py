"""Event intake: match workflows, check trigger conditions, insert runs. Nothing executes here;
the request only writes rows, so intake stays fast and survives worker outages."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models import Run, RunStatus, Workflow
from .conditions import evaluate


def new_context(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"event": event, "trigger": payload, "steps": {}, "vars": {}}


def create_run(session: Session, workflow: Workflow, event: str, payload: dict[str, Any],
               trigger_key: str | None = None) -> int | None:
    stmt = (
        insert(Run)
        .values(workflow_id=workflow.id, status=RunStatus.PENDING, current_step=0, step_count=0,
                context=new_context(event, payload), trigger_key=trigger_key)
        .on_conflict_do_nothing(constraint="uq_runs_workflow_trigger_key")
        .returning(Run.id)
    )
    return session.execute(stmt).scalar()


def dispatch_event(session: Session, event: str, payload: dict[str, Any],
                   idempotency_key: str | None = None) -> tuple[int, list[int], list[dict[str, Any]]]:
    workflows = session.scalars(
        select(Workflow).where(Workflow.enabled.is_(True), Workflow.trigger["event"].astext == event)
    ).all()
    run_ids: list[int] = []
    skipped: list[dict[str, Any]] = []
    for wf in workflows:
        ctx = new_context(event, payload)
        if not evaluate(wf.trigger.get("conditions"), ctx):
            skipped.append({"workflow_id": wf.id, "reason": "trigger conditions not met"})
            continue
        rid = create_run(session, wf, event, payload, idempotency_key)
        if rid is None:
            skipped.append({"workflow_id": wf.id, "reason": "duplicate event (idempotency_key)"})
        else:
            run_ids.append(rid)
    return len(workflows), run_ids, skipped
