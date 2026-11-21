"""Outbox dispatcher + message providers.

Enqueue happens inside the step's transaction (atomic with the step result).
Delivery happens after commit: lock pending rows with SKIP LOCKED, call the provider with
the idempotency key, mark sent. A crash between "provider accepted" and "marked sent"
means the row is delivered again later; the provider dedupes on the key, so the
recipient still sees the message once. That is the honest guarantee: at-least-once
attempts + idempotent delivery = effectively-once."""
from __future__ import annotations

import logging
import os
import threading
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_engine, session_scope
from ..models import Outbox

log = logging.getLogger("engine.outbox")

_delivery_count = 0
_count_lock = threading.Lock()


class LocalSinkProvider:
    """Stands in for an external provider (SES/Twilio/Slack). Separate connection and
    transaction from the engine, like a real remote system. Dedupes by idempotency key and
    counts attempts so tests can distinguish 'attempted twice' from 'delivered twice'."""

    def send(self, key: str, channel: str, recipient: str, payload: dict[str, Any]) -> None:
        with get_engine().begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO sink_messages (idempotency_key, channel, recipient, body, attempts)
                    VALUES (:k, :c, :r, :b, 1)
                    ON CONFLICT (idempotency_key)
                    DO UPDATE SET attempts = sink_messages.attempts + 1
                    """
                ),
                {"k": key, "c": channel, "r": recipient, "b": payload.get("body", "")},
            )


class WebhookProvider:
    def __init__(self, url: str) -> None:
        self.url = url

    def send(self, key: str, channel: str, recipient: str, payload: dict[str, Any]) -> None:
        r = httpx.post(self.url, json={"channel": channel, "to": recipient, **payload},
                       headers={"Idempotency-Key": key}, timeout=10)
        r.raise_for_status()


class SmtpEmailProvider:
    """Sends from the user's connected mailbox. Deterministic Message-ID from the outbox key."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def send(self, key: str, channel: str, recipient: str, payload: dict[str, Any]) -> None:
        from .. import email_smtp
        from ..crypto import decrypt
        from ..models import EmailAccount

        acct = self.session.get(EmailAccount, payload["account_id"])
        if acct is None:
            raise RuntimeError(f"email account {payload['account_id']} was deleted")
        cfg = email_smtp.SmtpConfig(
            host=acct.smtp_host, port=acct.smtp_port, security=acct.security,
            username=acct.username, password=decrypt(acct.password_encrypted) if acct.password_encrypted else None,
            from_email=acct.from_email, from_name=acct.from_name)
        msg = email_smtp.build_message(cfg, payload["to"], payload.get("subject") or "", payload.get("body") or "",
                                       cc=payload.get("cc"), message_id=email_smtp.message_id_for(key, acct.from_email))
        try:
            email_smtp.send(cfg, msg)
        except Exception as e:
            raise RuntimeError(email_smtp.friendly_error(e)) from e


def _providers(session: Session, payload: dict[str, Any]) -> list[Any]:
    if payload.get("account_id") is not None:  # real email
        return [SmtpEmailProvider(session)]
    providers: list[Any] = [LocalSinkProvider()]  # demo channel
    url = get_settings().message_webhook_url
    if url:
        providers.append(WebhookProvider(url))
    return providers


def _chaos_tick() -> None:
    """Test hook: die right after a successful delivery, before marking the row sent."""
    global _delivery_count
    limit = get_settings().chaos_exit_after_deliveries
    if not limit:
        return
    with _count_lock:
        _delivery_count += 1
        hit = _delivery_count == limit
    if hit:
        log.warning("CHAOS: hard exit after delivery #%s (outbox row NOT marked sent)", limit)
        os._exit(17)


def deliver_pending(limit: int = 50, keys: list[str] | None = None) -> int:
    """Deliver pending outbox rows. Safe to call concurrently from many workers."""
    sent = 0
    max_attempts = get_settings().outbox_max_attempts
    with session_scope() as s:
        q = (
            select(Outbox)
            .where(Outbox.status == "pending",
                   or_(Outbox.next_attempt_at.is_(None), Outbox.next_attempt_at <= func.now()))
            .order_by(Outbox.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if keys:
            q = q.where(Outbox.idempotency_key.in_(keys))
        rows = list(s.scalars(q))
        for row in rows:
            row.attempts += 1
            try:
                for p in _providers(s, row.payload):
                    p.send(row.idempotency_key, row.channel, row.recipient, row.payload)
            except Exception as e:
                row.last_error = str(e)[:1000]
                if row.attempts >= max_attempts:
                    row.status = "failed"
                    log.error("delivery of %s failed permanently after %s attempts: %s",
                              row.idempotency_key, row.attempts, e)
                else:  # exponential backoff: 10s, 20s, 40s, 80s, ...
                    row.next_attempt_at = func.now() + timedelta(seconds=5 * 2 ** row.attempts)
                    log.warning("delivery of %s failed (attempt %s), retrying: %s",
                                row.idempotency_key, row.attempts, e)
                continue
            _chaos_tick()
            row.status = "sent"
            row.sent_at = func.now()
            row.last_error = None
            sent += 1
    return sent


def enqueue(session: Session, run_id: int, step_id: str, msg: dict[str, Any]) -> None:
    """Insert into the outbox inside the caller's transaction; duplicate keys are a no-op."""
    from sqlalchemy.dialects.postgresql import insert

    session.execute(
        insert(Outbox)
        .values(
            idempotency_key=msg["idempotency_key"],
            run_id=run_id,
            step_id=step_id,
            channel=msg["channel"],
            recipient=msg["recipient"],
            payload=msg["payload"],
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
