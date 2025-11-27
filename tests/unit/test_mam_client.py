import json
import re
import pytest
from aioresponses import aioresponses
from aiohttp import ClientSession
from app.internal.clients.mam import (
    MyAnonamouseClient,
    MamClientSettings,
    AuthenticationError,
    SearchError,
)

@pytest.fixture
async def mam_client():
    async with ClientSession() as session:
        settings = MamClientSettings(mam_session_id="test_session_id")
        yield MyAnonamouseClient(session, settings)

@pytest.mark.asyncio
async def test_search_success(mam_client):
    mock_data = {
        "data": [
            {
                "id": 123,
                "title": "Test Book",
                "size": 1024,
                "seeders": 10,
                "leechers": 2,
                "added": "2023-01-01T12:00:00Z",
                "cat_name": "Audiobooks",
            }
        ]
    }
    
    with aioresponses() as m:
        m.post(re.compile(r"https://www\.myanonamouse\.net/tor/js/loadSearchJSONbasic\.php"), status=200, payload=mock_data)
        
        results = await mam_client.search("test query")
        
        assert len(results) == 1
        assert results[0].title == "Test Book"
        assert results[0].size == 1024
        assert results[0].seeders == 10

@pytest.mark.asyncio
async def test_search_auth_error(mam_client):
    with aioresponses() as m:
        m.post(re.compile(r"https://www\.myanonamouse\.net/tor/js/loadSearchJSONbasic\.php"), status=403)
        
        with pytest.raises(AuthenticationError):
            await mam_client.search("test query")

@pytest.mark.asyncio
async def test_search_empty(mam_client):
    mock_data = {"data": []}
    with aioresponses() as m:
        m.post(re.compile(r"https://www\.myanonamouse\.net/tor/js/loadSearchJSONbasic\.php"), status=200, payload=mock_data)
        
        results = await mam_client.search("test query")
        assert len(results) == 0

@pytest.mark.asyncio
async def test_search_mam_error(mam_client):
    mock_data = {"error": "Nothing returned"}
    with aioresponses() as m:
        m.post(re.compile(r"https://www\.myanonamouse\.net/tor/js/loadSearchJSONbasic\.php"), status=200, payload=mock_data)
        
        results = await mam_client.search("test query")
        assert len(results) == 0

@pytest.mark.asyncio
async def test_search_mam_real_error(mam_client):
    mock_data = {"error": "Something went wrong"}
    with aioresponses() as m:
        m.post(re.compile(r"https://www\.myanonamouse\.net/tor/js/loadSearchJSONbasic\.php"), status=200, payload=mock_data)
        
        with pytest.raises(SearchError, match="Something went wrong"):
            await mam_client.search("test query")

@pytest.mark.asyncio
async def test_download_torrent_success(mam_client):
    torrent_content = b"mock_torrent_content"
    with aioresponses() as m:
        m.get(re.compile(r"https://www\.myanonamouse\.net/torrents\.php.*"), status=200, body=torrent_content)
        
        content = await mam_client.download_torrent("123")
        assert content == torrent_content

@pytest.mark.asyncio
async def test_download_torrent_auth_error(mam_client):
    with aioresponses() as m:
        m.get("https://www.myanonamouse.net/torrents.php?action=download&id=123", status=403)
        m.get("https://www.myanonamouse.net/tor/download.php?id=123", status=403)
        m.get("https://www.myanonamouse.net/tor/download.php?tid=123", status=403)

        with pytest.raises(AuthenticationError):
            await mam_client.download_torrent("123")
