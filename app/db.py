"""
app/db.py
---------
Database plumbing - the equivalent of DbContext registration in .NET.

* engine        : the connection pool (one per app)
* SessionLocal  : factory for a unit-of-work Session (like a DbContext instance)
* get_db()      : FastAPI dependency giving each request its own Session
* init_db()     : creates folders and any missing tables (no migrations)
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import INSTANCE_DIR, UPLOAD_DIR, settings


class Base(DeclarativeBase):
    """Every model class in models.py inherits from this."""


def _make_engine() -> Engine:
    if settings.is_sqlite:
        INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        # FastAPI runs sync endpoints on a thread pool, so SQLite must allow
        # a connection to be used from a different thread than it was opened on.
        engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")   # SQLite ignores FKs unless told
            cur.execute("PRAGMA journal_mode=WAL")  # better concurrent reads
            cur.close()

        return engine

    # PostgreSQL (Neon free tier sleeps when idle, so test connections first).
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,   # silently reconnect if the server closed the link
        pool_recycle=300,     # recycle connections every 5 minutes
        pool_size=5,
        max_overflow=5,
    )


engine = _make_engine()

# expire_on_commit=False: objects stay readable after commit (handy in templates).
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """
    FastAPI dependency: `db: Session = Depends(get_db)`.
    The code after `yield` runs when the request finishes - like `using` in C#.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create folders and all tables that don't exist yet. Safe to run repeatedly."""
    from app import models  # noqa: F401  - importing registers the tables on Base

    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
