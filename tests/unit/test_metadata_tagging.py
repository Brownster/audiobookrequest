"""
Tests for metadata tagging to ensure Audiobookshelf compatibility.

This test suite verifies that:
1. Author tags are correctly mapped to 'artist' and 'album_artist'
2. Narrator tags are correctly mapped to 'composer'
3. Series information is properly extracted and tagged
"""

from datetime import datetime
from pathlib import Path

import pytest

from app.internal.models import BookRequest
from app.internal.processing.postprocess import PostProcessor


class TestMetadataExtraction:
    """Test metadata extraction from BookRequest into ffmpeg tags."""

    def test_extract_metadata_author_in_artist_field(self):
        """Test that author is correctly placed in artist field, not narrator."""
        processor = PostProcessor(
            output_dir=Path("/tmp/output"),
            tmp_dir=Path("/tmp/tmp"),
        )

        request = BookRequest(
            asin="B000TEST01",
            title="Harry Potter and the Philosopher's Stone",
            subtitle=None,
            authors=["J.K. Rowling"],
            narrators=["Stephen Fry"],
            cover_image="https://example.com/cover.jpg",
            release_date=datetime(2000, 1, 1),
            runtime_length_min=540,
        )

        metadata = processor._extract_metadata(request)
        ffmpeg_tags = metadata["ffmpeg_tags"]

        # CRITICAL: Artist must be the AUTHOR, not narrator
        assert ffmpeg_tags["artist"] == "J.K. Rowling"
        assert ffmpeg_tags["album_artist"] == "J.K. Rowling"
        # Narrator should be in composer field
        assert ffmpeg_tags["composer"] == "Stephen Fry"

    def test_extract_metadata_multiple_authors(self):
        """Test handling of multiple authors."""
        processor = PostProcessor(
            output_dir=Path("/tmp/output"),
            tmp_dir=Path("/tmp/tmp"),
        )

        request = BookRequest(
            asin="B000TEST02",
            title="Good Omens",
            subtitle=None,
            authors=["Terry Pratchett", "Neil Gaiman"],
            narrators=["Martin Jarvis"],
            cover_image=None,
            release_date=datetime(2000, 1, 1),
            runtime_length_min=600,
        )

        metadata = processor._extract_metadata(request)
        ffmpeg_tags = metadata["ffmpeg_tags"]

        # Primary author should be first author
        assert ffmpeg_tags["artist"] == "Terry Pratchett"
        assert ffmpeg_tags["album_artist"] == "Terry Pratchett"
        assert ffmpeg_tags["composer"] == "Martin Jarvis"

    def test_extract_metadata_multiple_narrators(self):
        """Test handling of multiple narrators."""
        processor = PostProcessor(
            output_dir=Path("/tmp/output"),
            tmp_dir=Path("/tmp/tmp"),
        )

        request = BookRequest(
            asin="B000TEST03",
            title="The Sandman",
            subtitle=None,
            authors=["Neil Gaiman"],
            narrators=["Neil Gaiman", "Kat Dennings", "James McAvoy"],
            cover_image=None,
            release_date=datetime(2020, 1, 1),
            runtime_length_min=660,
        )

        metadata = processor._extract_metadata(request)
        ffmpeg_tags = metadata["ffmpeg_tags"]

        # Author in artist
        assert ffmpeg_tags["artist"] == "Neil Gaiman"
        # All narrators in composer, comma-separated
        assert ffmpeg_tags["composer"] == "Neil Gaiman, Kat Dennings, James McAvoy"

    def test_extract_metadata_no_narrator(self):
        """Test handling when no narrator is provided."""
        processor = PostProcessor(
            output_dir=Path("/tmp/output"),
            tmp_dir=Path("/tmp/tmp"),
        )

        request = BookRequest(
            asin="B000TEST04",
            title="Test Book",
            subtitle=None,
            authors=["Test Author"],
            narrators=[],
            cover_image=None,
            release_date=datetime(2020, 1, 1),
            runtime_length_min=300,
        )

        metadata = processor._extract_metadata(request)
        ffmpeg_tags = metadata["ffmpeg_tags"]

        # Author still in artist
        assert ffmpeg_tags["artist"] == "Test Author"
        # Composer should be None when no narrators
        assert ffmpeg_tags["composer"] is None

    def test_extract_metadata_no_author(self):
        """Test fallback behavior when no author is provided."""
        processor = PostProcessor(
            output_dir=Path("/tmp/output"),
            tmp_dir=Path("/tmp/tmp"),
        )

        request = BookRequest(
            asin="B000TEST05",
            title="Unknown Book",
            subtitle=None,
            authors=[],
            narrators=["Unknown Narrator"],
            cover_image=None,
            release_date=datetime(2020, 1, 1),
            runtime_length_min=300,
        )

        metadata = processor._extract_metadata(request)
        ffmpeg_tags = metadata["ffmpeg_tags"]

        # When no authors, artist should be empty string (not narrator!)
        assert ffmpeg_tags["artist"] == ""
        assert ffmpeg_tags["album_artist"] == ""
        # Narrator still in composer
        assert ffmpeg_tags["composer"] == "Unknown Narrator"


class TestSeriesMetadata:
    """Test series metadata extraction and tagging."""

    def test_extract_series_metadata(self):
        """Test that series information is extracted into tags."""
        processor = PostProcessor(
            output_dir=Path("/tmp/output"),
            tmp_dir=Path("/tmp/tmp"),
        )

        request = BookRequest(
            asin="B000HARRY1",
            title="Harry Potter and the Philosopher's Stone",
            subtitle=None,
            authors=["J.K. Rowling"],
            narrators=["Stephen Fry"],
            series_name="Harry Potter",
            series_position="1",
            cover_image=None,
            release_date=datetime(2000, 1, 1),
            runtime_length_min=540,
        )

        metadata = processor._extract_metadata(request)
        ffmpeg_tags = metadata["ffmpeg_tags"]

        # Series tags for Audiobookshelf
        assert ffmpeg_tags["series"] == "Harry Potter"
        assert ffmpeg_tags["series-part"] == "1"
        # Also in metadata dict
        assert metadata["series_name"] == "Harry Potter"
        assert metadata["series_position"] == "1"

    def test_extract_series_metadata_with_decimal_position(self):
        """Test series with decimal positions (e.g., 2.5 for novellas)."""
        processor = PostProcessor(
            output_dir=Path("/tmp/output"),
            tmp_dir=Path("/tmp/tmp"),
        )

        request = BookRequest(
            asin="B000TEST06",
            title="A Novella",
            subtitle=None,
            authors=["Author Name"],
            narrators=["Narrator Name"],
            series_name="The Series",
            series_position="2.5",
            cover_image=None,
            release_date=datetime(2020, 1, 1),
            runtime_length_min=180,
        )

        metadata = processor._extract_metadata(request)
        ffmpeg_tags = metadata["ffmpeg_tags"]

        assert ffmpeg_tags["series"] == "The Series"
        assert ffmpeg_tags["series-part"] == "2.5"

    def test_extract_metadata_no_series(self):
        """Test behavior when no series information is available."""
        processor = PostProcessor(
            output_dir=Path("/tmp/output"),
            tmp_dir=Path("/tmp/tmp"),
        )

        request = BookRequest(
            asin="B000TEST07",
            title="Standalone Book",
            subtitle=None,
            authors=["Author Name"],
            narrators=["Narrator Name"],
            series_name=None,
            series_position=None,
            cover_image=None,
            release_date=datetime(2020, 1, 1),
            runtime_length_min=300,
        )

        metadata = processor._extract_metadata(request)
        ffmpeg_tags = metadata["ffmpeg_tags"]

        # Series tags should be None
        assert ffmpeg_tags["series"] is None
        assert ffmpeg_tags["series-part"] is None


class TestCompleteMetadata:
    """Integration tests for complete metadata extraction."""

    def test_complete_metadata_extraction_harry_potter(self):
        """
        Test complete metadata for a Harry Potter book to ensure
        Audiobookshelf can properly organize it.
        """
        processor = PostProcessor(
            output_dir=Path("/tmp/output"),
            tmp_dir=Path("/tmp/tmp"),
        )

        request = BookRequest(
            asin="B017V4IM1G",
            title="Harry Potter and the Chamber of Secrets",
            subtitle=None,
            authors=["J.K. Rowling"],
            narrators=["Jim Dale"],
            series_name="Harry Potter",
            series_position="2",
            cover_image="https://example.com/hp2.jpg",
            release_date=datetime(1999, 7, 2),
            runtime_length_min=540,
        )

        metadata = processor._extract_metadata(request)
        ffmpeg_tags = metadata["ffmpeg_tags"]

        # Verify all tags for Audiobookshelf compatibility
        assert ffmpeg_tags["title"] == "Harry Potter and the Chamber of Secrets"
        assert ffmpeg_tags["album"] == "Harry Potter and the Chamber of Secrets"
        assert ffmpeg_tags["artist"] == "J.K. Rowling"  # Author in artist!
        assert ffmpeg_tags["album_artist"] == "J.K. Rowling"
        assert ffmpeg_tags["composer"] == "Jim Dale"  # Narrator in composer
        assert ffmpeg_tags["series"] == "Harry Potter"
        assert ffmpeg_tags["series-part"] == "2"

    def test_metadata_includes_all_required_fields(self):
        """Test that metadata dict includes all fields needed for processing."""
        processor = PostProcessor(
            output_dir=Path("/tmp/output"),
            tmp_dir=Path("/tmp/tmp"),
        )

        request = BookRequest(
            asin="B000TEST08",
            title="Test Book",
            subtitle="A Subtitle",
            authors=["Author One", "Author Two"],
            narrators=["Narrator One"],
            series_name="Test Series",
            series_position="1",
            cover_image="https://example.com/cover.jpg",
            release_date=datetime(2020, 1, 1),
            runtime_length_min=420,
        )

        metadata = processor._extract_metadata(request)

        # Check all required fields exist
        assert metadata["title"] == "Test Book"
        assert metadata["authors"] == ["Author One", "Author Two"]
        assert metadata["narrators"] == ["Narrator One"]
        assert metadata["series_name"] == "Test Series"
        assert metadata["series_position"] == "1"
        assert metadata["asin"] == "B000TEST08"
        assert metadata["cover_url"] == "https://example.com/cover.jpg"
        assert metadata["publish_date"] == "2020-01-01T00:00:00"
        assert "ffmpeg_tags" in metadata
        assert "display_name" in metadata
