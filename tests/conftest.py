import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

# Ensure project root is on sys.path so `app` can be imported without editable install
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app
from app.util.db import get_session
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import GroupEnum

# Use in-memory SQLite for tests
TEST_DATABASE_URL = "sqlite://"

@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session

@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session
    app.dependency_overrides[get_session] = get_session_override

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
    client = TestClient(app)
    yield client
    ABRAuth.__call__ = original_auth_call  # type: ignore
    app.dependency_overrides.clear()
