import pytest
from unittest.mock import patch, MagicMock
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
    # Patch ABRAuth.__call__ to return the mock user
    with patch("app.internal.auth.authentication.ABRAuth.__call__", return_value=mock_user):
        yield client

@pytest.mark.asyncio
async def test_browse_mam_mock(authenticated_client):
    # Mock configuration
    with patch("app.routers.search.indexer_configuration_cache") as mock_cache:
        mock_cache.get.return_value = "test_session_id"
        
        response = authenticated_client.get("/search/browse/mam?mock=1")
        
        assert response.status_code == 200, f"Response: {response.text}"
        assert "MyAnonamouse Browse" in response.text
        assert "Mock Audiobook 1" in response.text

@pytest.mark.asyncio
async def test_browse_mam_category_filter(authenticated_client):
    with patch("app.routers.search.indexer_configuration_cache") as mock_cache:
        mock_cache.get.return_value = "test_session_id"
        
        # We are using mock=1, so the backend uses MOCK_RESULTS.
        # The mock client implementation in `MyAnonamouseClient.search` ignores categories when use_mock_data is True
        # effectively, but let's verify the route handles the param.
        
        response = authenticated_client.get("/search/browse/mam?mock=1&category=39")
        
        assert response.status_code == 200
        assert "MyAnonamouse Browse" in response.text
        # Verify the category is selected in the dropdown
        assert 'value="39" selected' in response.text

@pytest.mark.asyncio
async def test_search_mam_mock(authenticated_client):
    with patch("app.routers.search.indexer_configuration_cache") as mock_cache:
        mock_cache.get.return_value = "test_session_id"
        
        response = authenticated_client.get("/search/mam?q=test&mock=1")
        
        assert response.status_code == 200
        assert "Search Results" in response.text
        assert "Mock Audiobook 1" in response.text

@pytest.mark.asyncio
async def test_search_mam_no_session(authenticated_client):
    with patch("app.routers.search.indexer_configuration_cache") as mock_cache:
        mock_cache.get.return_value = None
        
        response = authenticated_client.get("/search/mam?q=test")
        
        # The code catches Exception and returns template with error.
        assert response.status_code == 200
        assert "Search failed" in response.text or "MAM session ID not configured" in response.text
