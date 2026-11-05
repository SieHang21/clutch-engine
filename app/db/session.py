"""
Clutch (ক্ল্যাচ) - Database Engine, Session Management & Lifecycle Hooks
Provides:
- SQLite connection string (default: sqlite:///./clutch.db) with check_same_thread=False
- SessionLocal factory and get_db() FastAPI dependency
- Automated table creation hook (init_db) on application startup
- set_engine() helper to allow in-memory SQLite isolation during tests
"""

import os
from typing import Generator
from contextlib import contextmanager
from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy.orm import sessionmaker

DEFAULT_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./clutch.db")

connect_args = {"check_same_thread": False} if DEFAULT_DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DEFAULT_DATABASE_URL,
    connect_args=connect_args,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, class_=Session)


def init_db(target_engine=None) -> None:
    """Create all database tables defined in SQLModel metadata."""
    import app.models.order  # noqa: F401
    eng = target_engine or engine
    SQLModel.metadata.create_all(eng)


def set_engine(new_engine) -> None:
    """Override engine (e.g. for test isolation with in-memory SQLite)."""
    global engine, SessionLocal
    engine = new_engine
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, class_=Session)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session and safely closes it."""
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


@contextmanager
def get_db_context() -> Generator[Session, None, None]:
    """Context manager yielding a database session for background workers or services."""
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()

