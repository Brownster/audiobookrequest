import sys
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

# Ensure project root is on sys.path so `app` can be imported without editable install
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Use in-memory SQLite for tests
TEST_DATABASE_URL = "sqlite://"

# Set up test database before importing app.main (which accesses DB on import)
_test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

# Import all models so SQLModel.metadata knows about them
from app.internal.models import (
    Config, User, BookRequest, ManualBookRequest,
    Notification, APIKey, DownloadJob
)

SQLModel.metadata.create_all(_test_engine)

# Override the database session for the entire test suite
from app.util.db import get_session

def _get_test_session():
    with Session(_test_engine) as session:
        yield session

# Must patch BEFORE importing app.main
import app.util.db
from contextlib import contextmanager

original_open_session = app.util.db.open_session

@contextmanager
def _test_open_session():
    with Session(_test_engine) as session:
        yield session

app.util.db.open_session = _test_open_session
# Also patch the engine used by get_session
app.util.db.engine = _test_engine

# Now safe to import app.main
from app.main import app
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import GroupEnum

@pytest.fixture(name="session")
def session_fixture():
    # Reuse the test engine created above
    with Session(_test_engine) as session:
        yield session

@pytest.fixture(name="client")
def client_fixture(session: Session):
    from aiohttp import ClientSession as AioClientSession
    from unittest.mock import AsyncMock
    from app.util.connection import get_connection
    from app.internal.services.download_manager import DownloadManager

    def get_session_override():
        return session
    app.dependency_overrides[get_session] = get_session_override

    # Override get_connection to provide a mock ClientSession (must be async generator)
    async def get_connection_override():
        mock_client = AsyncMock(spec=AioClientSession)
        yield mock_client
    app.dependency_overrides[get_connection] = get_connection_override

    # Bypass auth for tests with a mock admin user by patching ABRAuth.__call__
    mock_user = DetailedUser(
        username="testuser",
        group=GroupEnum.admin,
        root=False,
        extra_data=None,
        login_type="forms",
    )

    original_auth_call = ABRAuth.__call__

    async def _auth_call(self, request, session):
        return mock_user

    ABRAuth.__call__ = _auth_call  # type: ignore

    # Prevent DownloadManager from doing real startup/shutdown during tests
    dm = DownloadManager.get_instance()
    dm.start = AsyncMock()
    dm.stop = AsyncMock()

    # Bypass app lifespan tasks entirely to avoid background startup work
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop_lifespan(app):
        yield

    app.router.lifespan_context = _noop_lifespan

    client = TestClient(app)
    yield client
    ABRAuth.__call__ = original_auth_call  # type: ignore
    app.dependency_overrides.clear()
