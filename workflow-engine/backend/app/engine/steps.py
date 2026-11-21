"""Step-type and task registries.

A step handler is a function (step, ctx) -> StepResult. Handlers never touch the database
and never sleep for a `wait`: they *describe* what should happen (output, next step,
wake-up time, message to enqueue) and the executor persists it atomically."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..config import get_settings
from ..schemas import BranchStep, SendEmailStep, SendMessageStep, TaskStep, WaitStep, parse_duration
from .conditions import evaluate, render


@dataclass
class StepContext:
    run_id: int
    seq: int  # position of this step in the run's history (stable across retries)
    context: dict[str, Any]

    @property
    def idempotency_key(self) -> str:
        return f"{self.run_id}:{self.seq}"


@dataclass
class StepResult:
    output: dict[str, Any] = field(default_factory=dict)
    next: str | None = None  # step id / "end"; None = default successor
    wait_seconds: float | None = None
    outbox: dict[str, Any] | None = None  # {"idempotency_key", "channel", "recipient", "payload"}
    vars: dict[str, Any] | None = None  # merged into context["vars"]


StepHandler = Callable[[Any, StepContext], StepResult]
TaskHandler = Callable[[dict[str, Any], StepContext], dict[str, Any]]

STEP_HANDLERS: dict[str, StepHandler] = {}
TASKS: dict[str, TaskHandler] = {}


def step_type(name: str) -> Callable[[StepHandler], StepHandler]:
    def deco(fn: StepHandler) -> StepHandler:
        STEP_HANDLERS[name] = fn
        return fn
    return deco


def task(name: str) -> Callable[[TaskHandler], TaskHandler]:
    def deco(fn: TaskHandler) -> TaskHandler:
        TASKS[name] = fn
        return fn
    return deco


# --------------------------------------------------------------------------- step types


@step_type("task")
def run_task(step: TaskStep, ctx: StepContext) -> StepResult:
    handler = TASKS.get(step.task)
    if handler is None:
        raise ValueError(f"unknown task {step.task!r} (available: {sorted(TASKS)})")
    params = render(step.params, ctx.context)
    out = handler(params, ctx) or {}
    return StepResult(output=out, vars=out.pop("_vars", None) if isinstance(out, dict) else None)


@step_type("wait")
def run_wait(step: WaitStep, ctx: StepContext) -> StepResult:
    seconds = parse_duration(step.duration)
    scaled = seconds * get_settings().wait_time_scale
    return StepResult(
        output={"duration": step.duration, "seconds": seconds, "scaled_seconds": round(scaled, 3)},
        wait_seconds=scaled,
    )


@step_type("branch")
def run_branch(step: BranchStep, ctx: StepContext) -> StepResult:
    result = evaluate(step.condition, ctx.context)
    target = step.then if result else step.else_
    return StepResult(output={"result": result, "next": target or "(next)"}, next=target)


@step_type("send_message")
def run_send_message(step: SendMessageStep, ctx: StepContext) -> StepResult:
    recipient = str(render(step.to, ctx.context) or "")
    body = str(render(step.body, ctx.context) or "")
    subject = render(step.subject, ctx.context) if step.subject else None
    key = f"{ctx.run_id}:{step.id}:{ctx.seq}"
    payload = {"to": recipient, "subject": subject, "body": body}
    return StepResult(
        output={"idempotency_key": key, "channel": step.channel, "to": recipient, "queued": True},
        outbox={"idempotency_key": key, "channel": step.channel, "recipient": recipient, "payload": payload},
    )


def resolve_email_account_id(account_id: int | None) -> int:
    """Explicit account, else the default one, else the only one. Fails the step clearly."""
    from sqlalchemy import select

    from ..db import session_scope
    from ..models import EmailAccount

    with session_scope() as s:
        if account_id is not None:
            if s.get(EmailAccount, account_id) is None:
                raise ValueError(f"email account {account_id} not found")
            return account_id
        accounts = s.scalars(select(EmailAccount).order_by(EmailAccount.is_default.desc(), EmailAccount.id)).all()
        if not accounts:
            raise ValueError("no email account connected - add one via POST /email-accounts")
        return accounts[0].id


@step_type("send_email")
def run_send_email(step: SendEmailStep, ctx: StepContext) -> StepResult:
    account_id = resolve_email_account_id(step.account_id)
    to = str(render(step.to, ctx.context) or "").strip()
    if not to or "@" not in to:
        raise ValueError(f"invalid recipient {to!r} (template {step.to!r})")
    cc = str(render(step.cc, ctx.context) or "").strip() if step.cc else None
    subject = str(render(step.subject, ctx.context) or "")
    body = str(render(step.body, ctx.context) or "")
    key = f"{ctx.run_id}:{step.id}:{ctx.seq}"
    payload = {"account_id": account_id, "to": to, "cc": cc, "subject": subject, "body": body}
    return StepResult(
        output={"idempotency_key": key, "account_id": account_id, "to": to, "subject": subject, "queued": True},
        outbox={"idempotency_key": key, "channel": "email", "recipient": to, "payload": payload},
    )


# --------------------------------------------------------------------------- built-in tasks


@task("echo")
def task_echo(params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
    return dict(params)


@task("set")
def task_set(params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
    """Store values in context.vars for later steps."""
    return {**params, "_vars": dict(params)}


@task("score_order")
def task_score_order(params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
    amount = float(params.get("amount") or ctx.context.get("trigger", {}).get("amount") or 0)
    tier = "gold" if amount >= 1000 else "silver" if amount >= 500 else "bronze"
    return {"amount": amount, "tier": tier}


@task("simulate_work")
def task_simulate_work(params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
    """Synchronous CPU/IO work (NOT a wait). Capped so it can't hog a worker."""
    ms = min(int(params.get("ms", 200)), 10_000)
    time.sleep(ms / 1000)
    return {"worked_ms": ms}


@task("fail")
def task_fail(params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
    raise RuntimeError(params.get("message", "task failed on purpose"))


@task("http")
def task_http(params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
    """HTTP call. Sends an Idempotency-Key so a receiver can dedupe a retried call
    (tasks are at-least-once: a crash after the call but before commit re-runs it)."""
    url = params.get("url")
    if not url or not str(url).startswith(("http://", "https://")):
        raise ValueError("http task needs an http(s) url")
    method = str(params.get("method", "GET")).upper()
    headers = {"Idempotency-Key": ctx.idempotency_key, **(params.get("headers") or {})}
    timeout = min(float(params.get("timeout", 10)), 30)
    resp = httpx.request(method, url, headers=headers, json=params.get("json"),
                         content=params.get("body"), timeout=timeout)
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text[:2000]
    if resp.status_code >= 400 and not params.get("allow_failure"):
        raise RuntimeError(f"HTTP {resp.status_code} from {url}")
    return {"status": resp.status_code, "body": body}
