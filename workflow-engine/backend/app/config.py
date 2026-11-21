"""Runtime configuration, loaded from environment / backend/.env."""
from __future__ import annotations

import os
import socket
import uuid
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    database_url: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"

    # Worker
    run_worker: bool = True
    worker_concurrency: int = 8
    poll_interval_seconds: float = 1.0
    lease_seconds: float = 30.0
    max_steps_per_claim: int = 50
    # Multiplier applied to every `wait` duration (lets demos/tests run "1 day" in seconds).
    wait_time_scale: float = 1.0

    # Base URL used to build webhook URLs shown to users (set to your public/ngrok URL).
    public_base_url: str = "http://localhost:8000"
    # Fernet key for stored credentials; auto-generated into backend/.secret_key if empty.
    encryption_key: str = ""
    # Outbox delivery retries (exponential backoff) before a message is marked failed.
    outbox_max_attempts: int = 6

    # Delivery: if set, messages are POSTed here (with an Idempotency-Key header)
    # in addition to the local sink table.
    message_webhook_url: str = ""

    # Fault injection for the recovery test: hard-exit the process right after the
    # Nth successful provider delivery, *before* the outbox row is marked sent.
    chaos_exit_after_deliveries: int = 0

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url.strip().strip('"').strip("'")
        if not url:
            raise RuntimeError("DATABASE_URL is not set (put it in backend/.env)")
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
