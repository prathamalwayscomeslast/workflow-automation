"""Workflow definition schema. This is THE contract: the API, the YAML compiler and the
natural-language generator all produce a `WorkflowSpec`, and the engine only ever runs
definitions that validated against it."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

END = "end"

# --------------------------------------------------------------------------- conditions

Operator = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "exists", "not_exists"]


class Comparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(description="Dot path into the run context, e.g. 'trigger.amount' or 'amount'")
    op: Operator
    value: Any = None


class AllOf(BaseModel):
    model_config = ConfigDict(extra="forbid")
    all: list["Condition"]


class AnyOf(BaseModel):
    model_config = ConfigDict(extra="forbid")
    any: list["Condition"]


class Not(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    not_: "Condition" = Field(alias="not")


Condition = Union[Comparison, AllOf, AnyOf, Not]
AllOf.model_rebuild()
AnyOf.model_rebuild()
Not.model_rebuild()

# --------------------------------------------------------------------------- durations

_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)")


def parse_duration(value: str | int | float) -> float:
    """'1 day', '2h 30m', '45s', 90 -> seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    total, pos = 0.0, 0
    for m in _DURATION_RE.finditer(text):
        if text[pos:m.start()].strip(" ,and"):
            raise ValueError(f"invalid duration: {value!r}")
        unit = m.group(2)
        if unit not in _UNITS:
            raise ValueError(f"unknown duration unit {unit!r} in {value!r}")
        total += float(m.group(1)) * _UNITS[unit]
        pos = m.end()
    if pos == 0 or text[pos:].strip():
        raise ValueError(f"invalid duration: {value!r}")
    return total


# --------------------------------------------------------------------------- steps

StepId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_\-]{1,100}$")]


class StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StepId
    name: str | None = None
    # Optional explicit successor (step id or "end"); default is the next step in the list.
    next: str | None = None


class TaskStep(StepBase):
    type: Literal["task"] = "task"
    task: str = Field(description="Task name: http | echo | set | score_order | simulate_work")
    params: dict[str, Any] = Field(default_factory=dict)


class WaitStep(StepBase):
    type: Literal["wait"] = "wait"
    duration: str | float = Field(description="e.g. '1 day', '2 hours', '30s' or seconds as a number")

    @field_validator("duration")
    @classmethod
    def _valid(cls, v: str | float) -> str | float:
        if parse_duration(v) < 0:
            raise ValueError("duration must be >= 0")
        return v


class BranchStep(StepBase):
    type: Literal["branch"] = "branch"
    condition: Condition
    then: str = Field(description="Step id (or 'end') to jump to when the condition is true")
    else_: str | None = Field(default=None, alias="else", description="Step id/'end' when false; default: next step")
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SendMessageStep(StepBase):
    type: Literal["send_message"] = "send_message"
    channel: Literal["email", "sms", "slack", "webhook"] = "email"
    to: str = Field(description="Recipient; supports {{templates}} e.g. '{{trigger.email}}'")
    subject: str | None = None
    body: str = Field(description="Message text; supports {{templates}} from the run context")


Step = Annotated[Union[TaskStep, WaitStep, BranchStep, SendMessageStep], Field(discriminator="type")]


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: str = Field(min_length=1, description="Event type, e.g. 'order.created'")
    conditions: Condition | None = None


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    trigger: Trigger
    steps: list[Step] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _check_graph(self) -> "WorkflowSpec":
        ids = [s.id for s in self.steps]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate step ids: {sorted(dupes)}")
        if END in ids:
            raise ValueError("'end' is reserved and cannot be a step id")
        known = set(ids) | {END}
        for s in self.steps:
            targets = [s.next]
            if isinstance(s, BranchStep):
                targets += [s.then, s.else_]
            for t in targets:
                if t is not None and t not in known:
                    raise ValueError(f"step {s.id!r} points to unknown step {t!r}")
        return self

    def dump_steps(self) -> list[dict[str, Any]]:
        return [s.model_dump(by_alias=True, exclude_none=True, mode="json") for s in self.steps]

    def dump_trigger(self) -> dict[str, Any]:
        return self.trigger.model_dump(by_alias=True, exclude_none=True, mode="json")


# --------------------------------------------------------------------------- API models


class WorkflowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None
    trigger: dict[str, Any]
    steps: list[dict[str, Any]]
    source: str
    source_text: str | None = None
    enabled: bool
    created_at: datetime


class FromTextRequest(BaseModel):
    text: str = Field(min_length=3, max_length=4000)
    save: bool = True


class FromTextResponse(BaseModel):
    workflow: WorkflowOut | None
    spec: dict[str, Any]
    generator: Literal["claude", "rules"]
    notes: list[str] = []


class FromYamlRequest(BaseModel):
    yaml: str = Field(min_length=3, max_length=50_000)
    save: bool = True


class EventIn(BaseModel):
    event: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, description="Same key never starts the same workflow twice")


class EventOut(BaseModel):
    matched_workflows: int
    run_ids: list[int]
    skipped: list[dict[str, Any]] = []


class RunStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    seq: int
    step_index: int
    step_id: str
    step_type: str
    status: str
    output: dict[str, Any] | None
    error: str | None
    worker_id: str | None
    started_at: datetime
    finished_at: datetime


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    workflow_id: int
    status: str
    current_step: int | None
    step_count: int
    context: dict[str, Any]
    wake_at: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    claim_count: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class RunDetail(RunOut):
    steps: list[RunStepOut] = []
    workflow_steps: list[dict[str, Any]] = []
