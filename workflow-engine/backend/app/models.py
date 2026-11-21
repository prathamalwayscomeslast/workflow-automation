"""Durable state. Every bit of run progress lives in these tables, never in process memory."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RunStatus:
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # {"event": "order.created", "conditions": {...} | null}
    trigger: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Validated list of step definitions (see schemas.Step)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(20), default="json")  # json | text | yaml
    source_text: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # Every workflow can be started by POSTing to /hooks/{webhook_token}. The token is the secret.
    webhook_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Optional HMAC-SHA256 secret; when set, callers must send X-Signature-256: sha256=<hex>.
    webhook_secret: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_workflows_trigger_event", text("(trigger->>'event')")),)


class EmailAccount(Base):
    """A user's own mailbox, connected via SMTP + app password (n8n-style credential)."""

    __tablename__ = "email_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(100))
    from_email: Mapped[str] = mapped_column(String(300))
    from_name: Mapped[str | None] = mapped_column(String(200))
    smtp_host: Mapped[str] = mapped_column(String(200))
    smtp_port: Mapped[int] = mapped_column(Integer)
    security: Mapped[str] = mapped_column(String(10))  # ssl | starttls | none
    username: Mapped[str | None] = mapped_column(String(300))
    password_encrypted: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.PENDING)
    current_step: Mapped[int | None] = mapped_column(Integer, default=0)
    # Number of steps recorded so far; doubles as the sequence number of the next run_steps row.
    step_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # {"event": ..., "trigger": {payload}, "steps": {step_id: output}}
    context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # When the run becomes due (pending runs: creation time; waiting runs: end of the wait).
    wake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Optional caller-supplied event id: the same event never starts the same workflow twice.
    trigger_key: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_runs_status_wake_at", "status", "wake_at"),
        Index("ix_runs_status_lease", "status", "lease_expires_at"),
        Index("ix_runs_workflow", "workflow_id"),
        UniqueConstraint("workflow_id", "trigger_key", name="uq_runs_workflow_trigger_key"),
    )


class RunStep(Base):
    """Audit trail / timeline: one row per executed step."""

    __tablename__ = "run_steps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    step_index: Mapped[int] = mapped_column(Integer)
    step_id: Mapped[str] = mapped_column(String(100))
    step_type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20))  # completed | failed
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # A step result can only be committed once per position in the run's history.
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_steps_run_seq"),)


class Outbox(Base):
    """Transactional outbox for outgoing messages. idempotency_key makes enqueue a no-op on retry."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    step_id: Mapped[str] = mapped_column(String(100))
    channel: Mapped[str] = mapped_column(String(30))
    recipient: Mapped[str] = mapped_column(String(300))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_outbox_status", "status", "id"),)


class SinkMessage(Base):
    """The demo message provider ("external system"). It dedupes by idempotency key the
    way real providers (Stripe, Twilio, SES via headers) do, and counts every attempt so
    tests can prove a message was *delivered* once even if it was *attempted* twice."""

    __tablename__ = "sink_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    channel: Mapped[str] = mapped_column(String(30))
    recipient: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
