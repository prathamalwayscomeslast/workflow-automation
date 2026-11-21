"""FastAPI app. By default it also runs worker threads in-process (RUN_WORKER=true) so a single
`uvicorn app.main:app` is a complete demo; in production you'd run API and workers separately."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import init_db
from .engine.steps import TASKS
from .engine.worker import Worker
from .routes import runs, workflows

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    worker = None
    if get_settings().run_worker:
        worker = Worker()
        worker.start()
    yield
    if worker:
        worker.stop()


app = FastAPI(title="Workflow Automation Engine", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(workflows.router)
app.include_router(runs.router)


@app.get("/health")
def health() -> dict[str, object]:
    s = get_settings()
    return {"ok": True, "worker": s.run_worker, "nl_generator": "claude" if s.anthropic_api_key else "rules",
            "wait_time_scale": s.wait_time_scale, "tasks": sorted(TASKS)}
