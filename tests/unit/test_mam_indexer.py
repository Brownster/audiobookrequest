import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.internal.indexers.mam import MamIndexer, ValuedMamConfigurations
from app.internal.models import BookRequest, TorrentSource, BookMetadata

@pytest.fixture
def mock_container():
    container = MagicMock()
    container.client_session = AsyncMock()
    return container

@pytest.fixture
def mock_configurations():
    config = MagicMock(spec=ValuedMamConfigurations)
    config.mam_session_id = "test_session"
    config.use_mock_data = False
    return config

@pytest.fixture
def indexer():
    return MamIndexer()

@pytest.mark.asyncio
async def test_setup_success(indexer, mock_container, mock_configurations):
    request = BookRequest(title="Test Book", user_username="testuser", asin="B000000000", runtime_length_min=100, release_date=datetime.now())
    
    mock_result = MagicMock()
    mock_result.raw = {
        "id": 123,
        "title": "Test Book",
        "author_info": json.dumps({"1": "Author A"}),
    }
    
    with patch("app.internal.indexers.mam.MyAnonamouseClient") as MockClient:
        client_instance = MockClient.return_value
        # Ensure search is async
        client_instance.search = AsyncMock(return_value=[mock_result])
        
        await indexer.setup(request, mock_container, mock_configurations)
        
        assert "123" in indexer.results
        assert indexer.results["123"] == mock_result.raw
        client_instance.search.assert_called_once_with("Test Book", limit=100)

@pytest.mark.asyncio
async def test_setup_failure(indexer, mock_container, mock_configurations):
    request = BookRequest(title="Test Book", user_username="testuser", asin="B000000000", runtime_length_min=100, release_date=datetime.now())
    
    with patch("app.internal.indexers.mam.MyAnonamouseClient") as MockClient:
        client_instance = MockClient.return_value
        client_instance.search = AsyncMock(side_effect=Exception("Search failed"))
        
        await indexer.setup(request, mock_container, mock_configurations)
        
        assert len(indexer.results) == 0

@pytest.mark.asyncio
async def test_edit_source_metadata(indexer, mock_container):
    indexer.results = {
        "123": {
            "id": 123,
            "author_info": json.dumps({"1": "Author A"}),
            "narrator_info": json.dumps({"1": "Narrator B"}),
            "personal_freeleech": 1,
            "free": 0,
            "fl_vip": 0,
            "vip": 0,
            "filetype": "M4B",
        }
    }
    
    source = TorrentSource(
        guid="http://mam/123",
        indexer="MyAnonamouse",
        indexer_id=1,
        title="Test Book",
        size=1000,
        publish_date=datetime.now(),
        info_url="https://www.myanonamouse.net/t/123",
        book_metadata=BookMetadata(),
        indexer_flags=[],
        seeders=10,
        leechers=0
    )
    
    await indexer.edit_source_metadata(source, mock_container)
    
    assert source.book_metadata.authors == ["Author A"]
    assert source.book_metadata.narrators == ["Narrator B"]
    assert "personal_freeleech" in source.indexer_flags
    assert "freeleech" in source.indexer_flags
    assert source.book_metadata.filetype == "M4B"

@pytest.mark.asyncio
async def test_edit_source_metadata_no_match(indexer, mock_container):
    indexer.results = {}
    
    source = TorrentSource(
        guid="http://mam/999",
        indexer="MyAnonamouse",
        indexer_id=1,
        title="Test Book",
        size=1000,
        publish_date=datetime.now(),
        info_url="https://www.myanonamouse.net/t/999",
        book_metadata=BookMetadata(),
        indexer_flags=[],
        seeders=10,
        leechers=0
    )
    
    await indexer.edit_source_metadata(source, mock_container)
    
    assert source.book_metadata.authors == []
