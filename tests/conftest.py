"""
Pytest configuration and global test isolation fixtures.
Overrides database engine with an in-memory SQLite database using StaticPool
so all test operations and FastAPI dependencies operate against an isolated in-memory DB
without touching or polluting the local clutch.db file.
"""

import pytest
from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy.pool import StaticPool

import app.db.session as db_session
from app.db.session import get_db, set_engine, init_db
from app.main import app as fastapi_app
import app.models.order
from app.services.order_store import set_order_store, OrderStore


@pytest.fixture(autouse=True)
def isolated_db_environment():
    """
    Autouse fixture that creates a fresh in-memory SQLite database for each test,
    overrides the get_db dependency on the FastAPI application, and sets the active
    order repository.
    """
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    set_engine(test_engine)
    init_db(test_engine)

    def override_get_db():
        session = Session(test_engine)
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_db] = override_get_db
    store = OrderStore(session=None)
    set_order_store(store)

    yield test_engine

    fastapi_app.dependency_overrides.pop(get_db, None)
    SQLModel.metadata.drop_all(test_engine)
