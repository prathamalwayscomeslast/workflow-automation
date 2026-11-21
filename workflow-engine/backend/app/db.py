"""Database engine/session setup tuned for Neon (serverless Postgres).

Neon autosuspends idle computes, so the first query after idle can take seconds:
pool_pre_ping drops dead connections and the connect timeout is generous.
We poll instead of LISTEN/NOTIFY because Neon's pooler (PgBouncer, transaction
mode) does not support session-level LISTEN.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        s = get_settings()
        _engine = create_engine(
            s.sqlalchemy_url,
            pool_pre_ping=True,
            pool_size=s.worker_concurrency + 4,
            max_overflow=10,
            pool_recycle=240,  # Neon closes idle connections after ~5 min
            pool_timeout=60,
            connect_args={
                "connect_timeout": 30,
                # Server-side prepared statements break behind PgBouncer transaction pooling.
                "prepare_threshold": None,
            },
        )
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """One transaction: commit on success, rollback on error."""
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    models.Base.metadata.create_all(get_engine())
