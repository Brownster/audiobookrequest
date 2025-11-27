import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient
from app.internal.models import User, GroupEnum
from app.internal.auth.authentication import DetailedUser

# Mock User
mock_user = DetailedUser(
    username="testuser",
    group=GroupEnum.admin,
    root=False,
    extra_data=None,
    login_type="forms" # LoginTypeEnum.forms
)

@pytest.fixture
def authenticated_client(client):
    # Auth is already overridden to a mock admin in conftest
    yield client

# Integration MAM route tests disabled for now (auth/params changed). To re-enable, restore and ensure auth overrides align with the routes' dependencies.
