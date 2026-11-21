from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..engine.triggers import create_run
from ..models import Workflow
from ..nl import generate
from ..schemas import FromTextRequest, FromTextResponse, FromYamlRequest, WebhookSettings, WorkflowOut, WorkflowSpec
from ..yaml_compiler import compile_yaml

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _save(db: Session, spec: WorkflowSpec, source: str, source_text: str | None = None) -> Workflow:
    wf = Workflow(name=spec.name, description=spec.description, trigger=spec.dump_trigger(),
                  steps=spec.dump_steps(), source=source, source_text=source_text,
                  webhook_token=secrets.token_urlsafe(32))
    db.add(wf)
    db.flush()
    db.refresh(wf)
    return wf


def to_out(wf: Workflow) -> WorkflowOut:
    out = WorkflowOut.model_validate(wf)
    if wf.webhook_token:
        out.webhook_url = f"{get_settings().public_base_url.rstrip('/')}/hooks/{wf.webhook_token}"
    out.webhook_signed = bool(wf.webhook_secret)
    return out


def _spec_dict(spec: WorkflowSpec) -> dict[str, Any]:
    return {"name": spec.name, "description": spec.description,
            "trigger": spec.dump_trigger(), "steps": spec.dump_steps()}


@router.get("", response_model=list[WorkflowOut])
def list_workflows(db: Session = Depends(get_db)) -> list[WorkflowOut]:
    return [to_out(w) for w in db.scalars(select(Workflow).order_by(Workflow.id.desc()))]


@router.post("", response_model=WorkflowOut, status_code=201)
def create_workflow(spec: WorkflowSpec, db: Session = Depends(get_db)) -> WorkflowOut:
    return to_out(_save(db, spec, "json"))


@router.post("/validate")
def validate_workflow(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return {"valid": True, "spec": _spec_dict(WorkflowSpec.model_validate(body))}
    except ValidationError as e:
        return {"valid": False, "errors": e.errors(include_url=False, include_context=False)}


@router.post("/from-text", response_model=FromTextResponse)
def from_text(req: FromTextRequest, db: Session = Depends(get_db)) -> FromTextResponse:
    try:
        spec, generator, notes = generate(req.text)
    except (ValueError, ValidationError) as e:
        raise HTTPException(422, f"could not build a valid workflow: {e}")
    wf = _save(db, spec, "text", req.text) if req.save else None
    return FromTextResponse(workflow=to_out(wf) if wf else None,
                            spec=_spec_dict(spec), generator=generator, notes=notes)


@router.post("/from-yaml")
def from_yaml(req: FromYamlRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        spec = compile_yaml(req.yaml)
    except ValidationError as e:
        raise HTTPException(422, {"message": "compiled pipeline is invalid",
                                  "errors": e.errors(include_url=False, include_context=False)})
    except Exception as e:  # yaml.YAMLError, ValueError
        raise HTTPException(422, f"invalid pipeline YAML: {e}")
    wf = _save(db, spec, "yaml", req.yaml) if req.save else None
    return {"workflow": to_out(wf).model_dump(mode="json") if wf else None,
            "spec": _spec_dict(spec)}


@router.get("/{workflow_id}", response_model=WorkflowOut)
def get_workflow(workflow_id: int, db: Session = Depends(get_db)) -> WorkflowOut:
    wf = db.get(Workflow, workflow_id)
    if not wf:
        raise HTTPException(404, "workflow not found")
    return to_out(wf)


@router.put("/{workflow_id}", response_model=WorkflowOut)
def update_workflow(workflow_id: int, spec: WorkflowSpec, db: Session = Depends(get_db)) -> WorkflowOut:
    """Replace the definition. Runs already in flight continue with the new steps by index,
    so prefer creating a new workflow for structural changes (versioning is a next step)."""
    wf = db.get(Workflow, workflow_id)
    if not wf:
        raise HTTPException(404, "workflow not found")
    wf.name, wf.description = spec.name, spec.description
    wf.trigger, wf.steps = spec.dump_trigger(), spec.dump_steps()
    return to_out(wf)


@router.post("/{workflow_id}/webhook", response_model=WorkflowOut)
def webhook_settings(workflow_id: int, body: WebhookSettings, db: Session = Depends(get_db)) -> WorkflowOut:
    """Rotate the webhook URL and/or set (or clear with "") the HMAC signing secret."""
    wf = db.get(Workflow, workflow_id)
    if not wf:
        raise HTTPException(404, "workflow not found")
    if body.rotate_token or not wf.webhook_token:
        wf.webhook_token = secrets.token_urlsafe(32)
    if body.secret is not None:
        wf.webhook_secret = body.secret or None
    return to_out(wf)


@router.patch("/{workflow_id}", response_model=WorkflowOut)
def set_enabled(workflow_id: int, enabled: bool = Body(..., embed=True), db: Session = Depends(get_db)) -> WorkflowOut:
    wf = db.get(Workflow, workflow_id)
    if not wf:
        raise HTTPException(404, "workflow not found")
    wf.enabled = enabled
    return to_out(wf)


@router.delete("/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: int, db: Session = Depends(get_db)) -> None:
    wf = db.get(Workflow, workflow_id)
    if wf:
        db.delete(wf)


@router.post("/{workflow_id}/run")
def run_manually(workflow_id: int, payload: dict[str, Any] = Body(default_factory=dict),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    """Start a run directly, bypassing trigger conditions (handy for testing)."""
    wf = db.get(Workflow, workflow_id)
    if not wf:
        raise HTTPException(404, "workflow not found")
    rid = create_run(db, wf, wf.trigger.get("event", "manual"), payload)
    return {"run_id": rid}
