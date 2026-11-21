"""Connect the user's own mailbox (SMTP + app password). An account is only saved after a
real login + test email succeeds, so workflows never reference a broken mailbox."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import email_smtp
from ..crypto import decrypt, encrypt
from ..db import get_db
from ..models import EmailAccount
from ..schemas import EmailAccountIn, EmailAccountOut

router = APIRouter(prefix="/email-accounts", tags=["email"])


def _cfg(acct: EmailAccount, password: str | None = None) -> email_smtp.SmtpConfig:
    pw = password if password is not None else (decrypt(acct.password_encrypted) if acct.password_encrypted else None)
    return email_smtp.SmtpConfig(host=acct.smtp_host, port=acct.smtp_port, security=acct.security,
                                 username=acct.username, password=pw, from_email=acct.from_email,
                                 from_name=acct.from_name)


def _send_test(cfg: email_smtp.SmtpConfig, to: str) -> None:
    msg = email_smtp.build_message(
        cfg, to, "Workflow Engine: email connected",
        f"This is a test email from your Workflow Engine.\n\nWorkflows can now send email as {cfg.from_email}.")
    try:
        email_smtp.send(cfg, msg)
    except Exception as e:
        raise HTTPException(400, email_smtp.friendly_error(e))


@router.get("/presets")
def presets() -> dict[str, Any]:
    return email_smtp.PRESETS


@router.get("", response_model=list[EmailAccountOut])
def list_accounts(db: Session = Depends(get_db)) -> list[EmailAccount]:
    return list(db.scalars(select(EmailAccount).order_by(EmailAccount.id)))


@router.post("", response_model=EmailAccountOut, status_code=201)
def connect_account(body: EmailAccountIn, db: Session = Depends(get_db)) -> EmailAccount:
    preset = email_smtp.PRESETS[body.provider]
    host = body.smtp_host or preset.get("smtp_host")
    port = body.smtp_port or preset.get("smtp_port")
    security = body.security or preset.get("security") or "starttls"
    if not host or not port:
        raise HTTPException(422, "smtp_host and smtp_port are required for provider=custom")
    if security != "none" and not body.password:
        raise HTTPException(422, "password (app password) is required")
    password = (body.password or "").replace(" ", "") if body.provider in ("gmail", "yahoo") else body.password
    acct = EmailAccount(label=body.label, from_email=body.from_email, from_name=body.from_name,
                        smtp_host=str(host), smtp_port=int(port), security=security,
                        username=body.username or (body.from_email if password else None),
                        password_encrypted=encrypt(password) if password else None)
    _send_test(_cfg(acct, password), body.send_test_to or body.from_email)  # raises 400 on failure

    has_any = db.scalar(select(func.count()).select_from(EmailAccount))
    acct.is_default = body.is_default or not has_any
    if acct.is_default:
        db.execute(update(EmailAccount).values(is_default=False))
    acct.verified_at = func.now()
    db.add(acct)
    db.flush()
    db.refresh(acct)
    return acct


@router.post("/{account_id}/test")
def test_account(account_id: int, to: str | None = Body(default=None, embed=True),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    acct = db.get(EmailAccount, account_id)
    if not acct:
        raise HTTPException(404, "email account not found")
    _send_test(_cfg(acct), to or acct.from_email)
    acct.verified_at = func.now()
    return {"ok": True, "sent_to": to or acct.from_email}


@router.post("/{account_id}/default", response_model=EmailAccountOut)
def make_default(account_id: int, db: Session = Depends(get_db)) -> EmailAccount:
    acct = db.get(EmailAccount, account_id)
    if not acct:
        raise HTTPException(404, "email account not found")
    db.execute(update(EmailAccount).values(is_default=False))
    acct.is_default = True
    return acct


@router.delete("/{account_id}", status_code=204)
def delete_account(account_id: int, db: Session = Depends(get_db)) -> None:
    acct = db.get(EmailAccount, account_id)
    if acct:
        db.delete(acct)
