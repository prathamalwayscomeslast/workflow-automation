from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..engine.triggers import create_run
from ..models import Workflow
from ..nl import generate
from ..schemas import FromTextRequest, FromTextResponse, FromYamlRequest, WorkflowOut, WorkflowSpec
from ..yaml_compiler import compile_yaml

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _save(db: Session, spec: WorkflowSpec, source: str, source_text: str | None = None) -> Workflow:
    wf = Workflow(name=spec.name, description=spec.description, trigger=spec.dump_trigger(),
                  steps=spec.dump_steps(), source=source, source_text=source_text)
    db.add(wf)
    db.flush()
    db.refresh(wf)
    return wf


def _spec_dict(spec: WorkflowSpec) -> dict[str, Any]:
    return {"name": spec.name, "description": spec.description,
            "trigger": spec.dump_trigger(), "steps": spec.dump_steps()}


@router.get("", response_model=list[WorkflowOut])
def list_workflows(db: Session = Depends(get_db)) -> list[Workflow]:
    return list(db.scalars(select(Workflow).order_by(Workflow.id.desc())))


@router.post("", response_model=WorkflowOut, status_code=201)
def create_workflow(spec: WorkflowSpec, db: Session = Depends(get_db)) -> Workflow:
    return _save(db, spec, "json")


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
    return FromTextResponse(workflow=WorkflowOut.model_validate(wf) if wf else None,
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
    return {"workflow": WorkflowOut.model_validate(wf).model_dump(mode="json") if wf else None,
            "spec": _spec_dict(spec)}


@router.get("/{workflow_id}", response_model=WorkflowOut)
def get_workflow(workflow_id: int, db: Session = Depends(get_db)) -> Workflow:
    wf = db.get(Workflow, workflow_id)
    if not wf:
        raise HTTPException(404, "workflow not found")
    return wf


@router.patch("/{workflow_id}", response_model=WorkflowOut)
def set_enabled(workflow_id: int, enabled: bool = Body(..., embed=True), db: Session = Depends(get_db)) -> Workflow:
    wf = db.get(Workflow, workflow_id)
    if not wf:
        raise HTTPException(404, "workflow not found")
    wf.enabled = enabled
    return wf


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
