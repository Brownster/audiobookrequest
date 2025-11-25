"""Minimal tests for MAM cookie handling logic."""
import pytest
from aiohttp import ClientSession
from unittest.mock import MagicMock

from app.internal.clients.mam import MyAnonamouseClient, MamClientSettings


@pytest.fixture
def mock_session():
    """Mock aiohttp ClientSession."""
    return MagicMock(spec=ClientSession)


def test_cookie_kwargs_bare_token(mock_session):
    """Test that bare mam_id token is sent as cookie."""
    settings = MamClientSettings(mam_session_id="abc123def456")
    client = MyAnonamouseClient(mock_session, settings)

    result = client._cookie_kwargs()

    assert result == {"cookies": {"mam_id": "abc123def456"}}


def test_cookie_kwargs_full_header_with_equals(mock_session):
    """Test that cookie header with '=' is sent as header."""
    settings = MamClientSettings(
        mam_session_id="mam_id=abc123def456; other_cookie=value"
    )
    client = MyAnonamouseClient(mock_session, settings)

    result = client._cookie_kwargs()

    assert result == {
        "headers": {"Cookie": "mam_id=abc123def456; other_cookie=value"}
    }


def test_cookie_kwargs_full_header_with_semicolon(mock_session):
    """Test that cookie header with ';' is sent as header."""
    settings = MamClientSettings(mam_session_id="mam_id=xyz; path=/")
    client = MyAnonamouseClient(mock_session, settings)

    result = client._cookie_kwargs()

    assert result == {"headers": {"Cookie": "mam_id=xyz; path=/"}}


def test_cookie_kwargs_edge_case_equals_in_value(mock_session):
    """Test cookie with '=' inside the value."""
    settings = MamClientSettings(mam_session_id="token_with_=_char")
    client = MyAnonamouseClient(mock_session, settings)

    result = client._cookie_kwargs()

    # Should be treated as header since it contains '='
    assert result == {"headers": {"Cookie": "token_with_=_char"}}
