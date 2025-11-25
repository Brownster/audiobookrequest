"""Minimal tests for qBittorrent share limits functionality."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp import ClientSession

from app.internal.clients.torrent.qbittorrent import QbitClient, QbitCapabilities


@pytest.fixture
def mock_session():
    """Mock aiohttp ClientSession."""
    session = MagicMock(spec=ClientSession)
    return session


@pytest.fixture
def capabilities():
    """Mock capabilities for API v2."""
    return QbitCapabilities(
        api_major=2,
        supported_endpoints=frozenset(["/api/v2/app/webapiVersion"])
    )


@pytest.fixture
def qbit_client(mock_session, capabilities):
    """Create a QbitClient instance with mocked session."""
    client = QbitClient(
        http_session=mock_session,
        base_url="http://localhost:8080",
        username="admin",
        password="adminpass",
        capabilities=capabilities,
    )
    client._authenticated = True  # Skip auth for these tests
    return client


@pytest.mark.asyncio
async def test_set_share_limits_with_both_params(qbit_client, mock_session):
    """Test that set_share_limits calls API with both ratio and time."""
    # Mock the request method
    qbit_client._request = AsyncMock(return_value=None)

    await qbit_client.set_share_limits(
        "ABC123",
        ratio_limit=2.0,
        seeding_time_limit=4320,  # 72 hours in minutes
    )

    qbit_client._request.assert_called_once_with(
        "POST",
        "api/v2/torrents/setShareLimits",
        data={
            "hashes": "ABC123",
            "ratioLimit": "2.0",
            "seedingTimeLimit": "4320",
        },
    )


@pytest.mark.asyncio
async def test_set_share_limits_ratio_only(qbit_client):
    """Test that set_share_limits works with only ratio."""
    qbit_client._request = AsyncMock(return_value=None)

    await qbit_client.set_share_limits("ABC123", ratio_limit=1.5)

    qbit_client._request.assert_called_once_with(
        "POST",
        "api/v2/torrents/setShareLimits",
        data={
            "hashes": "ABC123",
            "ratioLimit": "1.5",
        },
    )


@pytest.mark.asyncio
async def test_set_share_limits_noop_when_no_limits(qbit_client):
    """Test that set_share_limits skips API call when no limits provided."""
    qbit_client._request = AsyncMock(return_value=None)

    await qbit_client.set_share_limits("ABC123")

    # Should not call the API
    qbit_client._request.assert_not_called()


@pytest.mark.asyncio
async def test_set_share_limits_time_only(qbit_client):
    """Test that set_share_limits works with only time limit."""
    qbit_client._request = AsyncMock(return_value=None)

    await qbit_client.set_share_limits("ABC123", seeding_time_limit=1440)

    qbit_client._request.assert_called_once_with(
        "POST",
        "api/v2/torrents/setShareLimits",
        data={
            "hashes": "ABC123",
            "seedingTimeLimit": "1440",
        },
    )
