"""Sending email from the user's own mailbox over SMTP.

Why SMTP + app password (for now): it works with Gmail, Outlook, Yahoo, Zoho and any custom
domain without registering an OAuth app or going through Google's restricted-scope review.
OAuth ("Sign in with Google") is the planned upgrade; the step/outbox contract won't change.

Duplicate safety: SMTP has no idempotency key. We set a deterministic Message-ID derived from
the outbox key, so if a crash forces a re-send, many clients/servers (including Gmail) collapse
the duplicate into one conversation item. The unavoidable window is "server accepted, we died
before marking the outbox row sent" - milliseconds wide, and documented."""
from __future__ import annotations

import hashlib
import html
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate

PRESETS: dict[str, dict[str, object]] = {
    "gmail": {"smtp_host": "smtp.gmail.com", "smtp_port": 465, "security": "ssl",
              "help": "Turn on 2-Step Verification, then create an app password at "
                      "https://myaccount.google.com/apppasswords (16 characters, no spaces)."},
    "outlook": {"smtp_host": "smtp-mail.outlook.com", "smtp_port": 587, "security": "starttls",
                "help": "Outlook.com: create an app password at https://account.live.com/proofs/AppPassword "
                        "(needs 2-step verification). Microsoft 365 work accounts often have SMTP AUTH disabled by the admin."},
    "yahoo": {"smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465, "security": "ssl",
              "help": "Account Security -> Generate app password."},
    "zoho": {"smtp_host": "smtp.zoho.com", "smtp_port": 465, "security": "ssl",
             "help": "Use an app-specific password if 2FA is on."},
    "custom": {"help": "Any SMTP server: host, port and security from your provider."},
}

TIMEOUT = 20


@dataclass
class SmtpConfig:
    host: str
    port: int
    security: str  # ssl | starttls | none
    username: str | None
    password: str | None
    from_email: str
    from_name: str | None = None


def message_id_for(key: str, from_email: str) -> str:
    domain = from_email.split("@", 1)[-1] or "workflow-engine.local"
    return f"<wf-{hashlib.sha256(key.encode()).hexdigest()[:32]}@{domain}>"


def _connect(cfg: SmtpConfig) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    if cfg.security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=TIMEOUT, context=ctx)
    else:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=TIMEOUT)
        server.ehlo()
        if cfg.security == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
    if cfg.username and cfg.password:
        try:
            server.login(cfg.username, cfg.password)
        except smtplib.SMTPServerDisconnected as e:
            # Gmail drops the connection (instead of replying 535) on some bad logins.
            raise smtplib.SMTPAuthenticationError(535, b"server closed the connection during login") from e
    return server


def build_message(cfg: SmtpConfig, to: str, subject: str, body: str, cc: str | None = None,
                  message_id: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.from_name or "", cfg.from_email))
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(body)
    msg.add_alternative(
        "<div style=\"font-family:system-ui,sans-serif;font-size:14px;line-height:1.5\">"
        + html.escape(body).replace("\n", "<br>") + "</div>", subtype="html")
    return msg


def send(cfg: SmtpConfig, msg: EmailMessage) -> None:
    with _connect(cfg) as server:
        server.send_message(msg)


def friendly_error(e: Exception) -> str:
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return ("Login rejected by the mail server. For Gmail/Outlook/Yahoo you must use an "
                "app password, not your normal password.")
    if isinstance(e, (TimeoutError, OSError)) and not isinstance(e, smtplib.SMTPException):
        return f"Could not reach the SMTP server ({e}). Check host, port and security."
    return f"{type(e).__name__}: {e}"
