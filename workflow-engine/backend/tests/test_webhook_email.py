"""End-to-end: webhook in -> workflow runs -> real email out over SMTP.

Uses a local SMTP server (aiosmtpd) as the user's "mailbox provider" and the real database
(DATABASE_URL). Runs its own worker in-process.

Stop any other API/worker on the same database first: an older worker would pick up these runs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from email import message_from_bytes
from pathlib import Path

import pytest

os.environ["RUN_WORKER"] = "false"  # the TestClient app must not start its own worker
os.environ["POLL_INTERVAL_SECONDS"] = "0.3"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402

if not get_settings().database_url:
    pytest.skip("DATABASE_URL not configured (backend/.env)", allow_module_level=True)

from aiosmtpd.controller import Controller  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.engine.worker import Worker  # noqa: E402
from app.main import app  # noqa: E402


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Inbox:
    def __init__(self) -> None:
        self.messages: list = []

    async def handle_DATA(self, server, session, envelope):  # aiosmtpd hook
        self.messages.append(message_from_bytes(envelope.content))
        return "250 OK"


@pytest.fixture(scope="module")
def smtp():
    inbox = Inbox()
    ctl = Controller(inbox, hostname="127.0.0.1", port=_free_port())
    ctl.start()
    port = ctl.port
    yield inbox, port
    ctl.stop()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:  # runs init_db via lifespan
        yield c


@pytest.fixture(scope="module")
def worker():
    w = Worker(concurrency=3)
    w.start()
    yield w
    w.stop()


def _wait(pred, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.4)
    raise AssertionError("timed out")


def test_webhook_triggers_email(client, smtp, worker):
    inbox, port = smtp
    tag = uuid.uuid4().hex[:8]

    # 1. Connect a mailbox (verification email is sent on save)
    r = client.post("/email-accounts", json={
        "label": f"test-{tag}", "provider": "custom", "from_email": f"shop-{tag}@example.com",
        "from_name": "Test Shop", "smtp_host": "127.0.0.1", "smtp_port": port, "security": "none",
    })
    assert r.status_code == 201, r.text
    account_id = r.json()["id"]
    assert any(f"shop-{tag}@example.com" in (m["From"] or "") for m in inbox.messages)
    n_before = len(inbox.messages)

    # 2. Workflow: webhook -> if amount > 500 -> email
    r = client.post("/workflows/from-yaml", json={"yaml": f"""
name: webhook email {tag}
on:
  if: amount > 500
steps:
  - id: thanks
    email:
      account: {account_id}
      to: "{{{{trigger.email}}}}"
      subject: "Order {{{{trigger.order_id}}}} received"
      body: "Hi {{{{trigger.name}}}}, thanks for your ${{{{trigger.amount}}}} order!"
"""})
    assert r.status_code == 200, r.text
    wf = r.json()["workflow"]
    hook = wf["webhook_url"].split("/hooks/")[1]
    path = f"/hooks/{hook}"

    # 3. Below threshold -> no run
    r = client.post(path, json={"amount": 100, "email": "x@example.com"})
    assert r.status_code == 202 and r.json()["started"] is False

    # 4. Fire the webhook (with an idempotency key, twice)
    body = {"order_id": f"o-{tag}", "amount": 750, "email": "ada@example.com", "name": "Ada"}
    r1 = client.post(path, json=body, headers={"Idempotency-Key": f"evt-{tag}"})
    r2 = client.post(path, json=body, headers={"Idempotency-Key": f"evt-{tag}"})
    assert r1.json()["started"] is True
    assert r2.json() == {"started": False, "reason": "duplicate Idempotency-Key", "workflow_id": wf["id"]}
    run_id = r1.json()["run_id"]

    # 5. Run completes and the email lands in the inbox exactly once
    _wait(lambda: client.get(f"/runs/{run_id}").json()["status"] == "completed")
    msgs = _wait(lambda: client.get(f"/runs/{run_id}/messages").json()
                 if client.get(f"/runs/{run_id}/messages").json()[0]["outbox_status"] == "sent" else None)
    assert msgs[0]["attempts"] == 1
    new = [m for m in inbox.messages[n_before:] if m["Subject"] == f"Order o-{tag} received"]
    assert len(new) == 1
    m = new[0]
    assert m["To"] == "ada@example.com"
    assert "Test Shop" in m["From"]
    assert m["Message-ID"].startswith("<wf-")
    text = m.get_body(preferencelist=("plain",)).get_content()
    assert "Hi Ada, thanks for your $750 order!" in text

    # 6. Signed webhooks: wrong signature rejected, correct one accepted
    client.post(f"/workflows/{wf['id']}/webhook", json={"secret": "s3cret"})
    raw = json.dumps({**body, "order_id": f"o2-{tag}"}).encode()
    bad = client.post(path, content=raw, headers={"content-type": "application/json", "X-Signature-256": "sha256=00"})
    assert bad.status_code == 401
    sig = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    ok = client.post(path, content=raw, headers={"content-type": "application/json", "X-Signature-256": sig})
    assert ok.json()["started"] is True
    _wait(lambda: client.get(f"/runs/{ok.json()['run_id']}").json()["status"] == "completed")

    client.delete(f"/workflows/{wf['id']}")
    client.delete(f"/email-accounts/{account_id}")


def test_send_email_without_account_fails_clearly(client, worker):
    tag = uuid.uuid4().hex[:8]
    r = client.post("/workflows", json={
        "name": f"no account {tag}", "trigger": {"event": "webhook"},
        "steps": [{"id": "mail", "type": "send_email", "account_id": 999999999,
                   "to": "a@example.com", "subject": "x", "body": "y"}]})
    wf = r.json()
    run_id = client.post(f"/hooks/{wf['webhook_url'].split('/hooks/')[1]}", json={}).json()["run_id"]
    run = _wait(lambda: (lambda j: j if j["status"] == "failed" else None)(client.get(f"/runs/{run_id}").json()))
    assert "email account 999999999 not found" in run["error"]
    client.delete(f"/workflows/{wf['id']}")
