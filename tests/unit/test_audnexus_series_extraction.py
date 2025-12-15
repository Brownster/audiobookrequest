"""
Tests for Audnexus API series extraction.

Verifies that series information from the Audnexus API is correctly
extracted and stored in BookRequest objects.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession

from app.internal.book_search import _get_audnexus_book


class TestAudnexusSeriesExtraction:
    """Test series extraction from Audnexus API responses."""

    @pytest.mark.asyncio
    async def test_extract_primary_series(self):
        """Test extraction of primary series information."""
        mock_response = {
            "asin": "B017V4IM1G",
            "title": "Harry Potter and the Chamber of Secrets",
            "subtitle": None,
            "authors": [{"name": "J.K. Rowling"}],
            "narrators": [{"name": "Jim Dale"}],
            "image": "https://example.com/cover.jpg",
            "releaseDate": "1999-07-02T00:00:00Z",
            "runtimeLengthMin": 540,
            "seriesPrimary": {
                "name": "Harry Potter",
                "asin": "B00SERIES1",
                "position": "2",
            },
        }

        session = MagicMock(spec=ClientSession)
        mock_resp = AsyncMock()
        mock_resp.ok = True
        mock_resp.json = AsyncMock(return_value=mock_response)
        session.get.return_value.__aenter__.return_value = mock_resp

        book = await _get_audnexus_book(session, "B017V4IM1G", "us")

        assert book is not None
        assert book.series_name == "Harry Potter"
        assert book.series_position == "2"
        assert book.title == "Harry Potter and the Chamber of Secrets"
        assert book.authors == ["J.K. Rowling"]
        assert book.narrators == ["Jim Dale"]

    @pytest.mark.asyncio
    async def test_extract_secondary_series_when_no_primary(self):
        """Test fallback to secondary series when primary is not available."""
        mock_response = {
            "asin": "B000TEST01",
            "title": "A Book in Secondary Series",
            "subtitle": None,
            "authors": [{"name": "Author Name"}],
            "narrators": [{"name": "Narrator Name"}],
            "image": None,
            "releaseDate": "2020-01-01T00:00:00Z",
            "runtimeLengthMin": 300,
            "seriesSecondary": {
                "name": "Secondary Series",
                "asin": "B00SERIES2",
                "position": "1",
            },
        }

        session = MagicMock(spec=ClientSession)
        mock_resp = AsyncMock()
        mock_resp.ok = True
        mock_resp.json = AsyncMock(return_value=mock_response)
        session.get.return_value.__aenter__.return_value = mock_resp

        book = await _get_audnexus_book(session, "B000TEST01", "us")

        assert book is not None
        assert book.series_name == "Secondary Series"
        assert book.series_position == "1"

    @pytest.mark.asyncio
    async def test_prefer_primary_over_secondary_series(self):
        """Test that primary series is preferred when both exist."""
        mock_response = {
            "asin": "B000TEST02",
            "title": "Book with Both Series",
            "subtitle": None,
            "authors": [{"name": "Author Name"}],
            "narrators": [{"name": "Narrator Name"}],
            "image": None,
            "releaseDate": "2020-01-01T00:00:00Z",
            "runtimeLengthMin": 300,
            "seriesPrimary": {
                "name": "Primary Series",
                "asin": "B00SERIES1",
                "position": "3",
            },
            "seriesSecondary": {
                "name": "Secondary Series",
                "asin": "B00SERIES2",
                "position": "1",
            },
        }

        session = MagicMock(spec=ClientSession)
        mock_resp = AsyncMock()
        mock_resp.ok = True
        mock_resp.json = AsyncMock(return_value=mock_response)
        session.get.return_value.__aenter__.return_value = mock_resp

        book = await _get_audnexus_book(session, "B000TEST02", "us")

        assert book is not None
        # Should use primary series
        assert book.series_name == "Primary Series"
        assert book.series_position == "3"

    @pytest.mark.asyncio
    async def test_no_series_information(self):
        """Test handling when no series information is present."""
        mock_response = {
            "asin": "B000TEST03",
            "title": "Standalone Book",
            "subtitle": None,
            "authors": [{"name": "Author Name"}],
            "narrators": [{"name": "Narrator Name"}],
            "image": None,
            "releaseDate": "2020-01-01T00:00:00Z",
            "runtimeLengthMin": 300,
        }

        session = MagicMock(spec=ClientSession)
        mock_resp = AsyncMock()
        mock_resp.ok = True
        mock_resp.json = AsyncMock(return_value=mock_response)
        session.get.return_value.__aenter__.return_value = mock_resp

        book = await _get_audnexus_book(session, "B000TEST03", "us")

        assert book is not None
        assert book.series_name is None
        assert book.series_position is None

    @pytest.mark.asyncio
    async def test_series_with_decimal_position(self):
        """Test handling of decimal series positions (e.g., novellas)."""
        mock_response = {
            "asin": "B000TEST04",
            "title": "A Novella Between Books",
            "subtitle": None,
            "authors": [{"name": "Author Name"}],
            "narrators": [{"name": "Narrator Name"}],
            "image": None,
            "releaseDate": "2020-01-01T00:00:00Z",
            "runtimeLengthMin": 180,
            "seriesPrimary": {
                "name": "The Main Series",
                "asin": "B00SERIES1",
                "position": "2.5",
            },
        }

        session = MagicMock(spec=ClientSession)
        mock_resp = AsyncMock()
        mock_resp.ok = True
        mock_resp.json = AsyncMock(return_value=mock_response)
        session.get.return_value.__aenter__.return_value = mock_resp

        book = await _get_audnexus_book(session, "B000TEST04", "us")

        assert book is not None
        assert book.series_name == "The Main Series"
        assert book.series_position == "2.5"

    @pytest.mark.asyncio
    async def test_series_extraction_full_harry_potter_example(self):
        """
        Test with a realistic Harry Potter example to ensure
        series detection works for a well-known series.
        """
        mock_response = {
            "asin": "B017V4NUPO",
            "title": "Harry Potter and the Philosopher's Stone",
            "subtitle": None,
            "authors": [{"name": "J.K. Rowling"}],
            "narrators": [{"name": "Stephen Fry"}],
            "image": "https://example.com/hp1.jpg",
            "releaseDate": "2000-01-01T00:00:00Z",
            "runtimeLengthMin": 540,
            "seriesPrimary": {
                "name": "Harry Potter",
                "asin": "B00HPSERIES",
                "position": "1",
            },
        }

        session = MagicMock(spec=ClientSession)
        mock_resp = AsyncMock()
        mock_resp.ok = True
        mock_resp.json = AsyncMock(return_value=mock_response)
        session.get.return_value.__aenter__.return_value = mock_resp

        book = await _get_audnexus_book(session, "B017V4NUPO", "us")

        assert book is not None
        assert book.title == "Harry Potter and the Philosopher's Stone"
        assert book.authors == ["J.K. Rowling"]
        assert book.narrators == ["Stephen Fry"]
        assert book.series_name == "Harry Potter"
        assert book.series_position == "1"
        # Ensure other fields still work
        assert book.asin == "B017V4NUPO"
        assert book.cover_image == "https://example.com/hp1.jpg"
        assert book.runtime_length_min == 540
