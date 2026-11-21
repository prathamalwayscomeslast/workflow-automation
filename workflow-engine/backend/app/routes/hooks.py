"""Webhook triggers: POST (or GET) /hooks/{token} starts the workflow that owns the token.

- JSON, form-encoded, or raw bodies are accepted; the parsed body becomes `trigger.*`.
  GET query parameters become `trigger.*` (handy for testing in a browser).
- Query string, method and selected headers are available as `webhook.*` in templates.
- If the workflow has a webhook secret, the request must carry
  `X-Signature-256: sha256=<hex HMAC-SHA256(secret, raw body)>` (GitHub's header name also works).
- `Idempotency-Key` header: the same key never starts the same workflow twice.
- Responds 202 immediately; nothing executes in the request."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from ..db import session_scope
from ..engine.conditions import evaluate
from ..engine.triggers import create_run, new_context
from ..models import Workflow

router = APIRouter(tags=["webhooks"])

MAX_BODY = 1_000_000
_FORWARD_HEADERS = ("user-agent", "content-type", "x-github-event", "x-request-id", "stripe-signature")


async def _parse(request: Request, raw: bytes) -> dict[str, Any]:
    if request.method == "GET":
        return dict(request.query_params)
    ctype = request.headers.get("content-type", "")
    if not raw:
        return {}
    if "json" in ctype or raw.lstrip()[:1] in (b"{", b"["):
        try:
            data = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "invalid JSON body")
    elif "form" in ctype:
        form = await request.form()
        data = {k: v for k, v in form.items() if isinstance(v, str)}
    else:
        data = {"body": raw.decode(errors="replace")}
    return data if isinstance(data, dict) else {"data": data}


def _verify_signature(secret: str, raw: bytes, request: Request) -> None:
    header = request.headers.get("x-signature-256") or request.headers.get("x-hub-signature-256") or ""
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(header.strip(), expected):
        raise HTTPException(401, "invalid or missing X-Signature-256")


def _start(token: str, raw: bytes, payload: dict[str, Any], meta: dict[str, Any],
           idem: str | None, request: Request) -> dict[str, Any]:
    with session_scope() as s:
        wf = s.scalar(select(Workflow).where(Workflow.webhook_token == token))
        if wf is None:
            raise HTTPException(404, "unknown webhook")
        if wf.webhook_secret:
            _verify_signature(wf.webhook_secret, raw, request)
        if not wf.enabled:
            return {"started": False, "reason": "workflow is disabled", "workflow_id": wf.id}
        ctx = new_context(wf.trigger.get("event", "webhook"), payload)
        ctx["webhook"] = meta
        if not evaluate(wf.trigger.get("conditions"), ctx):
            return {"started": False, "reason": "trigger conditions not met", "workflow_id": wf.id}
        run_id = create_run(s, wf, ctx["event"], payload, idem, extra={"webhook": meta})
        if run_id is None:
            return {"started": False, "reason": "duplicate Idempotency-Key", "workflow_id": wf.id}
        return {"started": True, "run_id": run_id, "workflow_id": wf.id}


@router.post("/hooks/{token}", status_code=202, operation_id="webhook_post")
@router.put("/hooks/{token}", status_code=202, operation_id="webhook_put")
@router.get("/hooks/{token}", status_code=202, operation_id="webhook_get")
async def receive_webhook(token: str, request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "payload too large (1 MB max)")
    payload = await _parse(request, raw)
    meta = {
        "method": request.method,
        "query": dict(request.query_params),
        "headers": {h: request.headers[h] for h in _FORWARD_HEADERS if h in request.headers},
    }
    idem = request.headers.get("idempotency-key") or request.headers.get("x-idempotency-key")
    return await run_in_threadpool(_start, token, raw, payload, meta, idem, request)
