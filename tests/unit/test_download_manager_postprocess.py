import asyncio
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlmodel import SQLModel, create_engine, Session
from sqlmodel.pool import StaticPool

from app.internal.models import (
    BookRequest,
    DownloadJob,
    DownloadJobStatus,
    Config,
)
from app.internal.services import download_manager
from app.internal.services.download_manager import DownloadManager
from app.internal.indexers.configuration import indexer_configuration_cache
from app.internal.processing.postprocess import PostProcessingError


class DummyPostProcessor:
    def __init__(self, destination: Path, record_snapshot: bool = True, raise_exc: Exception | None = None):
        self.destination = destination
        self.record_snapshot = record_snapshot
        self.last_snapshot = None
        self.raise_exc = raise_exc

    async def process(self, job_id: str, request: BookRequest, snapshot: dict) -> Path:
        if self.raise_exc:
            raise self.raise_exc
        if self.record_snapshot:
            self.last_snapshot = snapshot
        return self.destination


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.mark.asyncio
async def test_finalize_job_maps_remote_path(monkeypatch):
    engine = make_session()

    @contextmanager
    def _session_ctx():
        with Session(engine) as s:
            yield s

    # Inject in-memory session for open_session
    monkeypatch.setattr(download_manager, "open_session", _session_ctx)
    indexer_configuration_cache._cache.clear()

    remote_prefix = "/mnt/009/rapidseedbox65111/Downloads"
    local_prefix = "/home/marc/audiobookdownloads"

    with Session(engine) as s:
        # Store MAM/qB settings
        s.add(Config(key="MyAnonamouse_qbittorrent_remote_path_prefix", value=remote_prefix))
        s.add(Config(key="MyAnonamouse_qbittorrent_local_path_prefix", value=local_prefix))
        # Also store generic keys (used by create_valued_configuration without indexer prefix)
        s.add(Config(key="qbittorrent_remote_path_prefix", value=remote_prefix))
        s.add(Config(key="qbittorrent_local_path_prefix", value=local_prefix))
        s.add(Config(key="MyAnonamouse_mam_session_id", value="token"))
        s.commit()

        req = BookRequest(
            asin="ASIN123",
            title="Breakfast at Tiffany's",
            subtitle=None,
            authors=["Truman Capote"],
            narrators=[],
            cover_image=None,
            release_date=datetime.utcnow(),
            runtime_length_min=0,
        )
        s.add(req)
        s.commit()

        job = DownloadJob(
            request_id=req.id,
            title=req.title,
            torrent_id="123",
            status=DownloadJobStatus.processing,
            provider="qbittorrent",
            transmission_hash="hash123",
        )
        s.add(job)
        s.commit()
        job_id = job.id

    mgr = DownloadManager()
    dummy_dest = Path("/mnt/storage/audiobooks/Breakfast_at_Tiffanys.m4b")
    dummy_pp = DummyPostProcessor(destination=dummy_dest)
    mgr.postprocessor = dummy_pp
    mgr.torrent_client = None  # avoid qB file fetch
    from aiohttp import ClientSession
    session_http = ClientSession()
    mgr.http_session = session_http

    snapshot = {
        "downloadDir": f"{remote_prefix}/Breakfast at Tiffany's",
        "name": "Breakfast at Tiffany's",
        "files": [{"name": "Breakfast at Tiffany's/track1.mp3"}],
    }

    await mgr._finalize_job(str(job_id), snapshot)
    await session_http.close()

    with Session(engine) as s:
        db_job = s.get(DownloadJob, job_id)
        assert db_job.status == DownloadJobStatus.seeding
        assert db_job.destination_path == str(dummy_dest)
        assert "Processed" in (db_job.message or "")

    assert dummy_pp.last_snapshot is not None
    assert dummy_pp.last_snapshot.get("downloadDir") == f"{local_prefix}/Breakfast at Tiffany's"


@pytest.mark.asyncio
async def test_finalize_job_reports_postprocess_error(monkeypatch):
    engine = make_session()

    @contextmanager
    def _session_ctx():
        with Session(engine) as s:
            yield s

    monkeypatch.setattr(download_manager, "open_session", _session_ctx)
    indexer_configuration_cache._cache.clear()

    with Session(engine) as s:
        s.add(Config(key="MyAnonamouse_mam_session_id", value="token"))
        s.commit()

        req = BookRequest(
            asin="ASIN999",
            title="Test Book",
            subtitle=None,
            authors=["Author"],
            narrators=[],
            cover_image=None,
            release_date=datetime.utcnow(),
            runtime_length_min=0,
        )
        s.add(req)
        s.commit()

        job = DownloadJob(
            request_id=req.id,
            title=req.title,
            torrent_id="999",
            status=DownloadJobStatus.processing,
            provider="qbittorrent",
            transmission_hash="hash999",
        )
        s.add(job)
        s.commit()
        job_id = job.id

    mgr = DownloadManager()
    mgr.postprocessor = DummyPostProcessor(
        destination=Path("/tmp/out.m4b"),
        raise_exc=PostProcessingError("boom"),
    )
    mgr.torrent_client = None
    from aiohttp import ClientSession
    session_http = ClientSession()
    mgr.http_session = session_http

    snapshot = {
        "downloadDir": "/unmapped/path",
        "name": "Test Book",
        "files": [],
    }

    await mgr._finalize_job(str(job_id), snapshot)
    await session_http.close()

    with Session(engine) as s:
        db_job = s.get(DownloadJob, job_id)
        # Status should remain as seeding even when post-processing fails
        # to allow torrent to continue seeding on private trackers
        assert db_job.status == DownloadJobStatus.seeding
        assert db_job.message and "Post-processing failed" in db_job.message
