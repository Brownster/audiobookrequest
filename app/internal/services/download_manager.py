from __future__ import annotations

import asyncio
import math
from pathlib import Path
import uuid
from datetime import datetime
from typing import Optional, cast
from urllib.parse import urljoin
from datetime import timedelta

from aiohttp import ClientSession
from sqlmodel import select, Session, col
from torf import Torrent as TorfTorrent

from app.util.log import logger
from app.util.db import open_session
from app.internal.models import DownloadJob, DownloadJobStatus, BookRequest, MediaType
from app.internal.clients.mam import MamClientSettings, MyAnonamouseClient
from app.internal.clients.torrent.abstract import AbstractTorrentClient
from app.internal.clients.torrent.transmission import TransmissionClient
from app.internal.clients.torrent.qbittorrent import QbitClient
from app.internal.processing.postprocess import (
    PostProcessor,
    PostProcessingError,
    EbookPostProcessor,
)
from app.internal.services.seeding import build_seed_configuration, TorrentSeedConfiguration
from app.internal.env_settings import Settings
from app.internal.indexers.configuration import create_valued_configuration
from app.internal.indexers.mam import MamIndexer, ValuedMamConfigurations
from app.internal.indexers.abstract import SessionContainer
from app.internal.indexers.configuration import indexer_configuration_cache

def _ensure_directory(path_str: str) -> Path:
    path = Path(path_str)
    path.mkdir(parents=True, exist_ok=True)
    return path

# We need to define a settings adapter or use the global settings
# For now, we'll assume some settings are available in env_settings or we default them.
# We might need to extend Settings in env_settings.py to include MAM/Torrent configs.

class DownloadManager:
    _instance: Optional[DownloadManager] = None
    _instance_lock: asyncio.Lock = asyncio.Lock()

    def __init__(self):
        self.queue: asyncio.Queue[str] = asyncio.Queue() # Queue of Job IDs
        self.worker_task: Optional[asyncio.Task] = None
        self.monitor_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._postprocess_lock = asyncio.Lock()
        self._job_lock = asyncio.Lock()  # Lock for job state transitions
        self._postprocess_sweep_running = False
        self.http_session: Optional[ClientSession] = None
        self._last_mam_retry: Optional[datetime] = None
        self._last_postprocess_sweep: Optional[datetime] = None
        self._last_seed_cleanup: Optional[datetime] = None
        
        # We'll initialize these in start()
        self.mam_client: Optional[MyAnonamouseClient] = None
        self.torrent_client: Optional[AbstractTorrentClient] = None
        self.postprocessor: Optional[PostProcessor] = None
        self.ebook_postprocessor: Optional[EbookPostProcessor] = None
        
        # Settings placeholders (should be loaded from config)
        settings = Settings().app
        self.download_dir = getattr(settings, "download_dir", "/tmp/abr/audiobooks")
        self.book_dir = getattr(settings, "book_dir", "/tmp/abr/books")
        self.postprocess_tmp_dir = "/tmp/abr/mam-service"
        self.transmission_url = "http://transmission:9091/transmission/rpc"
        self.mam_session_id = ""

    @classmethod
    def get_instance(cls) -> DownloadManager:
        """Get or create the singleton instance (sync version for compatibility)."""
        if cls._instance is None:
            cls._instance = DownloadManager()
        return cls._instance

    @classmethod
    async def get_instance_async(cls) -> DownloadManager:
        """Get or create the singleton instance (async version, thread-safe)."""
        if cls._instance is None:
            async with cls._instance_lock:
                # Double-check pattern
                if cls._instance is None:
                    cls._instance = DownloadManager()
        return cls._instance

    async def start(self):
        if self.worker_task:
            return

        self.http_session = ClientSession()
        
        # Ensure directories exist
        self.download_dir = str(_ensure_directory(self.download_dir))
        self.book_dir = str(_ensure_directory(self.book_dir))
        self.postprocess_tmp_dir = str(_ensure_directory(self.postprocess_tmp_dir))

        # Initialize PostProcessor
        self.postprocessor = PostProcessor(
            output_dir=Path(self.download_dir), # This should be the final destination
            tmp_dir=Path(self.postprocess_tmp_dir),
            http_session=self.http_session
        )
        self.ebook_postprocessor = EbookPostProcessor(
            output_dir=Path(self.book_dir),
            tmp_dir=Path(self.postprocess_tmp_dir),
            http_session=self.http_session,
        )

        # Preload settings and test torrent client connection if possible
        try:
            with open_session() as session:
                container = SessionContainer(
                    session=session,
                    client_session=self.http_session or ClientSession(),
                )
                mam_config_def = await MamIndexer.get_configurations(container)
                config = cast(ValuedMamConfigurations, create_valued_configuration(mam_config_def, session, check_required=False))
                client_type = config.download_client or "transmission"
                # Prefer qBittorrent if URL is set; fall back to Transmission only if qB is not configured
                qbit_url = getattr(config, "qbittorrent_url", None)
                if client_type == "qbittorrent" or qbit_url:
                    qbit_url = qbit_url or "http://qbittorrent:8080"
                    qbit_user = getattr(config, "qbittorrent_username", "") or ""
                    qbit_pass = getattr(config, "qbittorrent_password", "") or ""
                    self.torrent_client = QbitClient(self.http_session, qbit_url, qbit_user, qbit_pass)
                else:
                    trans_url = config.transmission_url or "http://transmission:9091/transmission/rpc"
                    trans_user = config.transmission_username
                    trans_pass = config.transmission_password
                    self.torrent_client = TransmissionClient(self.http_session, trans_url, trans_user, trans_pass)
                await self.torrent_client.test_connection()
                logger.info("DownloadManager: torrent client connection OK", provider=client_type)
                if not config.mam_session_id:
                    logger.warning("DownloadManager: MAM session ID not configured")
        except Exception as exc:
            logger.warning("DownloadManager: startup validation skipped/failed", error=str(exc))

        self.worker_task = asyncio.create_task(self._worker(), name="mam-worker")
        self.monitor_task = asyncio.create_task(self._monitor(), name="mam-monitor")
        logger.info("DownloadManager started")

    async def stop(self):
        self._stopping = True
        if self.worker_task:
            self.worker_task.cancel()
        if self.monitor_task:
            self.monitor_task.cancel()
        if self.http_session:
            await self.http_session.close()
        await asyncio.gather(
            *(t for t in (self.worker_task, self.monitor_task) if t), return_exceptions=True
        )

    async def submit_job(self, job_id: str) -> None:
        await self.queue.put(job_id)

    async def _worker(self):
        while not self._stopping:
            try:
                job_id = await self.queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._process_job(job_id)
            except Exception as exc:
                logger.exception("DownloadManager: job failed", job_id=job_id)
                with open_session() as session:
                    job_uuid = self._coerce_uuid(job_id)
                    job = session.get(DownloadJob, job_uuid) if job_uuid else None
                    if job:
                        job.status = DownloadJobStatus.failed
                        job.message = f"Job failed: {exc}"
                        session.add(job)
                        session.commit()
            finally:
                self.queue.task_done()

    def _coerce_uuid(self, value: str | uuid.UUID | None) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except Exception:
            logger.error("DownloadManager: invalid job id", job_id=value)
            return None

    async def _process_job(self, job_id: str):
        job_uuid = self._coerce_uuid(job_id)
        if job_uuid is None:
            return
        with open_session() as session:
            job = session.get(DownloadJob, job_uuid)
            if not job:
                return
            if not job.request_id:
                job.status = DownloadJobStatus.failed
                job.message = "Download job missing linked request"
                session.add(job)
                session.commit()
                return
            
            # Fetch settings using helper
            container = SessionContainer(session=session, client_session=self.http_session)
            mam_config_def = await MamIndexer.get_configurations(container)
            try:
                # We cast to ValuedMamConfigurations for type hinting, though it's dynamically created
                config = cast(ValuedMamConfigurations, create_valued_configuration(mam_config_def, session, check_required=False))
            except Exception as e:
                logger.error("DownloadManager: failed to load settings", error=str(e))
                job.status = DownloadJobStatus.failed
                job.message = "Failed to load MAM settings"
                session.add(job)
                session.commit()
                return

            mam_session_id = config.mam_session_id or indexer_configuration_cache.get(session, "MyAnonamouse_mam_session_id")
            
            if not mam_session_id:
                logger.error("DownloadManager: MAM session ID not configured")
                job.status = DownloadJobStatus.failed
                job.message = "MAM session ID not configured"
                session.add(job)
                session.commit()
                return

            # Init MAM Client
            mam_settings = MamClientSettings(
                mam_session_id=mam_session_id,
                use_mock_data=config.use_mock_data or False
            )
            self.mam_client = MyAnonamouseClient(self.http_session, mam_settings)
            
            # Init Torrent Client (force qBittorrent)
            qbit_url = config.qbittorrent_url or "http://qbittorrent:8080"
            qbit_user = config.qbittorrent_username or ""
            qbit_pass = config.qbittorrent_password or ""
            self.torrent_client = QbitClient(self.http_session, qbit_url, qbit_user, qbit_pass)
            client_type = "qbittorrent"

            job.status = DownloadJobStatus.downloading
            job.message = "Downloading torrent metadata"
            session.add(job)
            session.commit()
            
            try:
                if not job.torrent_id:
                     raise ValueError("Job has no torrent_id")

                torrent_bytes = await self.mam_client.download_torrent(job.torrent_id)
                
                # Determine seed config
                seed_config = build_seed_configuration(job, config) # type: ignore
                
                # Add to client
                # We might need to pass specific options for Qbit vs Transmission
                # AbstractTorrentClient.add_torrent takes **kwargs
                add_result = await self.torrent_client.add_torrent(
                    torrent_bytes, 
                    download_dir=self.download_dir,
                    seed_config=seed_config,
                    expected_name=job.title,
                    expected_tag=f"mamid={job.torrent_id}" if job.torrent_id else None,
                )
                
                if add_result:
                    job.transmission_hash = add_result.get("hashString") or add_result.get("hash") or job.transmission_hash
                    job.transmission_id = add_result.get("id")
                if not job.transmission_hash:
                    raise RuntimeError("Failed to register torrent with client (no hash returned)")
                job.seed_configuration = seed_config.to_record()
                job.status = DownloadJobStatus.seeding # Assume it starts seeding/downloading
                job.message = f"Added to {client_type}"
                job.provider = client_type
                session.add(job)
                session.commit()
                
                # Apply limits if Qbit
                if isinstance(self.torrent_client, QbitClient) and job.transmission_hash:
                    await self.torrent_client.set_share_limits(
                        job.transmission_hash,
                        ratio_limit=seed_config.ratio_limit,
                        seeding_time_limit=seed_config.seeding_time_limit,
                    )

            except Exception as exc:
                logger.exception("DownloadManager: failed to process job", job_id=job_id)
                job.status = DownloadJobStatus.failed
                job.message = f"Processing failed: {exc}"
                session.add(job)
                session.commit()
    async def _monitor(self):
        while not self._stopping:
            try:
                await asyncio.sleep(60)
                await self._maybe_retry_unavailable()
                await self._poll_torrents()
                await self._maybe_finalize_seeding()
                await self._maybe_cleanup_seeded()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("DownloadManager: monitor failed", error=str(exc))

    async def _maybe_finalize_seeding(self):
        """Every 15 minutes, sequentially post-process any seeding jobs that never finalized."""
        if self._stopping:
            return
        now = datetime.utcnow()
        if self._last_postprocess_sweep and (now - self._last_postprocess_sweep).total_seconds() < 900:
            return
        if self._postprocess_sweep_running:
            return
        self._postprocess_sweep_running = True
        try:
            with open_session() as session:
                # Refresh client if missing
                if not self.torrent_client:
                    container = SessionContainer(session=session, client_session=self.http_session)
                    mam_config_def = await MamIndexer.get_configurations(container)
                    try:
                        config = cast(ValuedMamConfigurations, create_valued_configuration(mam_config_def, session, check_required=False))
                        client_type = config.download_client or "transmission"
                        if client_type == "qbittorrent":
                            self.torrent_client = QbitClient(
                                self.http_session,
                                config.qbittorrent_url or "http://qbittorrent:8080",
                                config.qbittorrent_username or "",
                                config.qbittorrent_password or "",
                            )
                        else:
                            self.torrent_client = TransmissionClient(
                                self.http_session,
                                config.transmission_url or "http://transmission:9091/transmission/rpc",
                                config.transmission_username,
                                config.transmission_password,
                            )
                    except Exception:
                        pass

                # Grab seeding/processing jobs with no destination yet
                jobs = session.exec(
                    select(DownloadJob).where(
                        DownloadJob.status.in_([DownloadJobStatus.seeding, DownloadJobStatus.processing]),
                        DownloadJob.destination_path == None,  # noqa: E711
                    )
                    .order_by(DownloadJob.created_at.asc())
                ).all()
                if not jobs or not self.torrent_client:
                    return

                for job in jobs:
                    if not job.transmission_hash:
                        continue
                    try:
                        torrents = await self.torrent_client.get_torrents([job.transmission_hash])
                        snapshot = torrents.get(job.transmission_hash) or next(iter(torrents.values()), {})
                        if not snapshot:
                            continue
                        await self._finalize_job(str(job.id), snapshot)
                    except Exception as exc:
                        logger.warning("DownloadManager: postprocess sweep skipped job", job_id=str(job.id), error=str(exc))
        finally:
            self._last_postprocess_sweep = datetime.utcnow()
            self._postprocess_sweep_running = False

    async def _maybe_cleanup_seeded(self):
        """Every 15 minutes, remove torrents that have passed the seed target after processing."""
        if self._stopping:
            return
        now = datetime.utcnow()
        if self._last_seed_cleanup and (now - self._last_seed_cleanup).total_seconds() < 900:
            return
        self._last_seed_cleanup = now

        with open_session() as session:
            # Ensure client exists
            if not self.torrent_client:
                container = SessionContainer(session=session, client_session=self.http_session)
                try:
                    mam_config_def = await MamIndexer.get_configurations(container)
                    config = cast(ValuedMamConfigurations, create_valued_configuration(mam_config_def, session, check_required=False))
                    self.torrent_client = QbitClient(
                        self.http_session,
                        config.qbittorrent_url or "http://qbittorrent:8080",
                        config.qbittorrent_username or "",
                        config.qbittorrent_password or "",
                    )
                except Exception:
                    return

            # Find processed jobs with a destination that have exceeded seed target hours
            jobs = session.exec(
                select(DownloadJob).where(
                    DownloadJob.status == DownloadJobStatus.seeding,
                    DownloadJob.destination_path.is_not(None),
                    DownloadJob.completed_at.is_not(None),
                )
            ).all()
            for job in jobs:
                try:
                    if not job.completed_at:
                        continue
                    seed_target = 0
                    if job.seed_configuration:
                        cfg = TorrentSeedConfiguration.from_record(job.seed_configuration)
                        if cfg:
                            seed_target = cfg.required_seed_seconds
                    # Fall back to 0 if not set
                    if seed_target <= 0:
                        continue
                    elapsed = (datetime.utcnow() - job.completed_at).total_seconds()
                    if elapsed < seed_target:
                        continue
                    if not job.transmission_hash:
                        continue
                    # Remove torrent but keep data
                    try:
                        await self.torrent_client.remove_torrent(job.transmission_hash, delete_data=False)
                        job.message = "Seed target met; torrent removed"
                        session.add(job)
                        session.commit()
                    except Exception as exc:
                        logger.warning("Seed cleanup: failed to remove torrent", job_id=str(job.id), error=str(exc))
                except Exception as exc:
                    logger.debug("Seed cleanup: skipped job", job_id=str(job.id) if job.id else "", error=str(exc))

    async def _maybe_retry_unavailable(self):
        """Periodically retry MAM searches for wishlist items previously marked unavailable."""
        # Only run once an hour to avoid hammering MAM
        now = datetime.utcnow()
        if self._last_mam_retry and (now - self._last_mam_retry).total_seconds() < 3600:
            return
        self._last_mam_retry = now

        retry_interval = timedelta(hours=72)
        with open_session() as session:
            mam_config_def = await MamIndexer.get_configurations(
                SessionContainer(session=session, client_session=self.http_session)
            )
            try:
                config = cast(
                    ValuedMamConfigurations,
                    create_valued_configuration(mam_config_def, session, check_required=False),
                )
            except Exception:
                return

            mam_session_id = config.mam_session_id or indexer_configuration_cache.get(session, "MyAnonamouse_mam_session_id")
            if not mam_session_id:
                return

            cutoff = now - retry_interval
            pending = session.exec(
                select(BookRequest).where(
                    BookRequest.downloaded == False,  # noqa: E712
                    BookRequest.mam_unavailable == True,  # noqa: E712
                    col(BookRequest.user_username).is_not(None),
                    (BookRequest.mam_last_check == None)  # noqa: E711
                    | (BookRequest.mam_last_check <= cutoff),
                )
            ).all()

            if not pending:
                return

            client = MyAnonamouseClient(
                self.http_session,
                MamClientSettings(mam_session_id=mam_session_id, use_mock_data=config.use_mock_data or False),
            )

            for request in pending[:5]:  # limit per sweep
                query = request.title
                if request.authors:
                    query = f"{request.title} {', '.join(request.authors)}"
                try:
                    results = await client.search(query, limit=40)
                except Exception as exc:
                    logger.warning("MAM retry: search failed", error=str(exc), request_id=str(request.id))
                    request.mam_last_check = now
                    session.add(request)
                    session.commit()
                    continue

                if not results:
                    request.mam_last_check = now
                    session.add(request)
                    session.commit()
                    continue

                best = max(results, key=lambda r: (r.seeders, r.peers, -r.size))
                torrent_id = str(best.raw.get("id") or best.guid.split("-")[-1])
                if not torrent_id:
                    request.mam_last_check = now
                    session.add(request)
                    session.commit()
                    continue

                # Avoid duplicate active jobs
                existing_job = session.exec(
                    select(DownloadJob).where(
                        DownloadJob.request_id == request.id,
                        DownloadJob.torrent_id == torrent_id,
                        DownloadJob.status.in_(
                            [
                                DownloadJobStatus.pending,
                                DownloadJobStatus.downloading,
                                DownloadJobStatus.seeding,
                                DownloadJobStatus.processing,
                            ]
                        ),
                    )
                ).first()
                if existing_job:
                    request.mam_unavailable = False
                    request.mam_last_check = now
                    session.add(request)
                    session.commit()
                    continue

                job = DownloadJob(
                    request_id=request.id,
                    title=best.title or request.title,
                    torrent_id=torrent_id,
                    status=DownloadJobStatus.pending,
                    provider="qbittorrent",
                    message="Queued via MAM retry",
                )
                request.mam_unavailable = False
                request.mam_last_check = now
                session.add(request)
                session.add(job)
                session.commit()

                await self.queue.put(str(job.id))

    async def _poll_torrents(self):
        # We need to re-init client here if it's not set, or just rely on process_job having set it.
        # But process_job might not have run if we restarted.
        # So we should probably init client in _poll_torrents too if missing.
        
        with open_session() as session:
            if not self.torrent_client:
                 # Try to init client
                container = SessionContainer(session=session, client_session=self.http_session)
                mam_config_def = await MamIndexer.get_configurations(container)
                try:
                    config = cast(ValuedMamConfigurations, create_valued_configuration(mam_config_def, session, check_required=False))
                    client_type = config.download_client or "transmission"
                    if client_type == "qbittorrent":
                        qbit_url = config.qbittorrent_url or "http://qbittorrent:8080"
                        qbit_user = config.qbittorrent_username or ""
                        qbit_pass = config.qbittorrent_password or ""
                        self.torrent_client = QbitClient(self.http_session, qbit_url, qbit_user, qbit_pass)
                    else:
                        trans_url = config.transmission_url or "http://transmission:9091/transmission/rpc"
                        trans_user = config.transmission_username
                        trans_pass = config.transmission_password
                        self.torrent_client = TransmissionClient(self.http_session, trans_url, trans_user, trans_pass)
                except Exception:
                    pass

            # Include "failed" jobs so we can auto-correct jobs that failed post-processing but are still seeding
            jobs = session.exec(
                select(DownloadJob).where(
                    DownloadJob.status.in_([DownloadJobStatus.downloading, DownloadJobStatus.seeding, DownloadJobStatus.failed]),
                    DownloadJob.transmission_hash.is_not(None)
                )
            ).all()

            if not jobs:
                return

            hashes = [j.transmission_hash for j in jobs if j.transmission_hash]
            if not hashes:
                return

            try:
                torrents = await self.torrent_client.get_torrents(hashes)
            except Exception as exc:
                logger.error("DownloadManager: failed to get torrents", error=str(exc))
                return
            
            for job in jobs:
                if not job.transmission_hash:
                    continue
                
                t_info = torrents.get(job.transmission_hash)
                if not t_info:
                    # Torrent missing?
                    continue

                # Auto-correct failed jobs that are still active in qBittorrent
                # (e.g., post-processing failed but torrent is still seeding)
                if job.status == DownloadJobStatus.failed:
                    job.status = DownloadJobStatus.seeding
                    logger.info(
                        "DownloadManager: auto-corrected failed job to seeding",
                        job_id=str(job.id),
                        hash=job.transmission_hash,
                    )

                # Update seed stats
                job.seed_seconds = job.seed_seconds or 0
                # Transmission reports secondsSeeding; qB uses seeding_time
                elapsed = t_info.get("seeding_time") or t_info.get("secondsSeeding") or 0
                if isinstance(elapsed, (int, float)):
                    # Clamp to reasonable range (0 to 1 year in seconds) to prevent overflow
                    MAX_SEED_SECONDS = 365 * 24 * 3600  # 1 year
                    elapsed = max(0, min(int(elapsed), MAX_SEED_SECONDS))
                    job.seed_seconds = max(job.seed_seconds, elapsed)

                # Determine completion
                state = t_info.get("state") or t_info.get("status") or ""
                state_lower = str(state).lower()
                if state:
                    logger.debug(
                        "DownloadManager: monitor torrent state",
                        hash=job.transmission_hash,
                        state=state,
                        status=job.status.value if hasattr(job.status, "value") else str(job.status),
                    )
                if state:
                    # Set a more accurate message/status based on qBittorrent state
                    # Separate inactive states by download/upload phase
                    inactive_downloading_states = {"pauseddl", "queueddl", "stalleddl", "checkingdl"}
                    inactive_seeding_states = {"pausedup", "queuedup", "stalledup", "checkingup"}
                    downloading_states = {"downloading", "forceddl", "metadl", "allocating"}
                    uploading_states = {"uploading", "forcedup"}

                    if state_lower in downloading_states:
                        job.status = DownloadJobStatus.downloading
                        job.message = f"qB state: {state}"
                    elif state_lower in uploading_states:
                        job.status = DownloadJobStatus.seeding
                        job.message = f"qB state: {state}"
                    elif state_lower in inactive_downloading_states:
                        # Inactive while downloading - resume but don't force-start yet
                        job.status = DownloadJobStatus.downloading
                        job.message = f"qB state: {state} (inactive) → resuming"
                        try:
                            await self.torrent_client.resume(job.transmission_hash)
                            logger.info("DownloadManager: resumed inactive downloading torrent", hash=job.transmission_hash, state=state)
                        except Exception as exc:
                            logger.warning("DownloadManager: failed to resume inactive downloading torrent", error=str(exc))
                            job.message = f"qB state: {state} (resume failed)"
                    elif state_lower in inactive_seeding_states:
                        # Inactive while seeding - force-start to ensure active seeding
                        job.status = DownloadJobStatus.seeding
                        job.message = f"qB state: {state} (inactive) → force-starting"
                        try:
                            if isinstance(self.torrent_client, QbitClient):
                                # Force-start is the most aggressive way to ensure seeding
                                await self.torrent_client.force_start(job.transmission_hash)
                                logger.info("DownloadManager: force-started inactive seeding torrent", hash=job.transmission_hash, state=state)
                                job.message = f"qB state: force-started (was {state})"
                            else:
                                # Fallback to resume for non-qB clients
                                await self.torrent_client.resume(job.transmission_hash)
                                logger.info("DownloadManager: resumed inactive seeding torrent", hash=job.transmission_hash, state=state)
                        except Exception as exc:
                            logger.warning("DownloadManager: failed to force-start inactive seeding torrent", error=str(exc), hash=job.transmission_hash)
                            job.message = f"qB state: {state} (force-start failed)"

                    # Persist state and message updates
                    session.add(job)

                left_until_done = t_info.get("leftUntilDone")
                progress = t_info.get("progress")
                is_downloaded = False
                if left_until_done is not None:
                    is_downloaded = left_until_done == 0
                elif progress is not None:
                    try:
                        is_downloaded = float(progress) >= 1.0
                    except Exception:
                        is_downloaded = False
                # If qB reports seeding/paused-up, treat as downloaded
                if state_lower in {"uploading", "forcedup", "pausedup", "stalledup"}:
                    is_downloaded = True

                required_seed = 0
                ratio_limit = None
                if job.seed_configuration:
                    seed_config = TorrentSeedConfiguration.from_record(job.seed_configuration)
                    if seed_config:
                        required_seed = seed_config.required_seed_seconds
                        ratio_limit = seed_config.ratio_limit

                # Ratio
                current_ratio = t_info.get("ratio") or t_info.get("uploadRatio") or 0
                try:
                    current_ratio = float(current_ratio)
                except Exception:
                    current_ratio = 0

                meets_seed_time = required_seed == 0 or job.seed_seconds >= required_seed
                meets_ratio = ratio_limit is None or (current_ratio >= ratio_limit)

                # Check if we should move to completed (post-processing done AND seed time met)
                # Use job lock to prevent race conditions with _finalize_job
                async with self._job_lock:
                    # Re-fetch job state within lock to ensure consistency
                    job = session.get(DownloadJob, job.id)
                    if not job:
                        continue

                    if (
                        job.status == DownloadJobStatus.seeding
                        and job.destination_path  # Post-processing completed
                        and meets_seed_time
                        and meets_ratio
                    ):
                        # Seed time requirement met - remove torrent and mark as completed
                        try:
                            await self.torrent_client.remove_torrent(job.transmission_hash)
                            logger.info(
                                "DownloadManager: removed torrent after seed time met",
                                job_id=str(job.id),
                                hash=job.transmission_hash,
                                seed_seconds=job.seed_seconds,
                                required_seed=required_seed,
                            )
                            job.status = DownloadJobStatus.completed
                            job.message = f"Seeded for {job.seed_seconds // 3600}h - completed"
                            if not job.completed_at:
                                job.completed_at = datetime.utcnow()
                            session.add(job)
                        except Exception as exc:
                            logger.warning(
                                "DownloadManager: failed to remove torrent after seed completion",
                                error=str(exc),
                                hash=job.transmission_hash,
                            )
                            # Don't update status if removal failed - will retry next cycle
                    elif is_downloaded and meets_seed_time and meets_ratio and job.status != DownloadJobStatus.processing:
                        # Start post-processing (but keep seeding)
                        job.status = DownloadJobStatus.processing
                        job.message = "Download complete, starting processing"
                        session.add(job)
                        session.commit()

                        # Trigger finalization in background
                        asyncio.create_task(self._finalize_job(str(job.id), t_info))

            # Commit all state/message updates for monitored jobs
            session.commit()

    async def _finalize_job(self, job_id: str, torrent_snapshot: dict):
        async with self._postprocess_lock, self._job_lock:
            job_uuid = self._coerce_uuid(job_id)
            if job_uuid is None:
                return
            with open_session() as session:
                job = session.get(DownloadJob, job_uuid)
                if not job:
                    return
                
                request = session.get(BookRequest, job.request_id) if job.request_id else None
                if not request:
                    logger.error("Cannot finalize job without request", job_id=job_id)
                    return

                # Reload settings so we can apply any path mapping
                container = SessionContainer(session=session, client_session=self.http_session)
                mam_config_def = await MamIndexer.get_configurations(container)
                config = cast(
                    ValuedMamConfigurations,
                    create_valued_configuration(mam_config_def, session, check_required=False),
                )

                # Normalize the snapshot so the postprocessor can find the files locally
                snapshot = dict(torrent_snapshot)
                download_dir = (
                    snapshot.get("downloadDir")
                    or snapshot.get("save_path")
                    or snapshot.get("savepath")
                )
                if not download_dir:
                    content_path = snapshot.get("content_path")
                    if content_path and isinstance(content_path, str) and content_path.strip():
                        # Validate content_path before using it
                        content_path = content_path.strip()
                        if content_path and content_path != "/" and len(content_path) > 1:
                            download_dir = str(Path(content_path).parent)

                remote_prefix = (config.qbittorrent_remote_path_prefix or "").rstrip("/")
                local_prefix = (config.qbittorrent_local_path_prefix or "").rstrip("/")

                # Fallback to global qB settings if MAM-specific prefixes are unset
                if not remote_prefix:
                    remote_prefix = (indexer_configuration_cache.get(session, "qbittorrent_remote_path_prefix") or "").rstrip("/")
                if not local_prefix:
                    local_prefix = (indexer_configuration_cache.get(session, "qbittorrent_local_path_prefix") or "").rstrip("/")
                if (
                    download_dir
                    and remote_prefix
                    and local_prefix
                    and str(download_dir).startswith(remote_prefix)
                ):
                    suffix = str(download_dir)[len(remote_prefix):] if remote_prefix != "/" else str(download_dir)
                    download_dir = str(Path(local_prefix) / suffix.lstrip("/"))
                    logger.info(
                        "DownloadManager: mapped remote path",
                        remote_prefix=remote_prefix,
                        local_prefix=local_prefix,
                        mapped_path=download_dir,
                    )

                if download_dir:
                    snapshot["downloadDir"] = download_dir

                # qBittorrent needs an extra call to get file list
                if isinstance(self.torrent_client, QbitClient):
                    try:
                        snapshot["files"] = await self.torrent_client.list_files(job.transmission_hash)
                        if snapshot["files"]:
                            # Use the common prefix of file paths as name fallback
                            names = [Path(f.get("name", "")) for f in snapshot["files"] if isinstance(f.get("name"), str)]
                            if names:
                                # First part of the first path is the torrent root folder
                                root_part = names[0].parts[0] if names[0].parts else None
                                if root_part:
                                    snapshot.setdefault("name", root_part)
                    except Exception as exc:
                        logger.warning(
                            "DownloadManager: unable to fetch qBittorrent file list",
                            error=str(exc),
                            hash=job.transmission_hash,
                        )

                try:
                    if not snapshot.get("downloadDir"):
                        raise PostProcessingError(
                            "No download path reported by the torrent client. Set the qB Remote/Local path mapping in Settings ▸ MAM so files can be located."
                        )
                    processor = (
                        self.ebook_postprocessor
                        if job.media_type == MediaType.ebook
                        else self.postprocessor
                    )
                    if not processor:
                        raise PostProcessingError("Post-processor not initialized")

                    destination = await asyncio.wait_for(
                        processor.process(str(job.id), request, snapshot),
                        timeout=30 * 60,  # 30 minutes
                    )
                    # Keep status as seeding to reflect ongoing seeding on private trackers
                    job.status = DownloadJobStatus.seeding
                    job.destination_path = str(destination)
                    job.message = f"Processed -> {destination}"
                    job.completed_at = datetime.utcnow()
                    session.add(job)
                    session.commit()
                    
                    # Cleanup torrent
                    # await self.torrent_client.remove_torrent(job.transmission_hash)
                    
                except asyncio.TimeoutError:
                    # Keep status as seeding since torrent is still active
                    job.status = DownloadJobStatus.seeding
                    job.message = "Post-processing failed: Timed out after 30 minutes"
                    session.add(job)
                    session.commit()
                except Exception as exc:
                    # Keep status as seeding since torrent is still active
                    job.status = DownloadJobStatus.seeding
                    job.message = f"Post-processing failed: {exc}"
                    session.add(job)
                    session.commit()

    async def reprocess_job(self, job_id: str) -> bool:
        """Manually trigger a retry. If the torrent was never added, requeue download; otherwise retry post-processing."""
        if not self.http_session:
            self.http_session = ClientSession()

        job_uuid = self._coerce_uuid(job_id)
        if job_uuid is None:
            return False

        snapshot = {}
        with open_session() as session:
            job = session.get(DownloadJob, job_uuid)
            if not job:
                return False

            job.completed_at = None

            # If we never registered a torrent hash, requeue from the start
            if not job.transmission_hash:
                job.status = DownloadJobStatus.pending
                job.message = "Retrying download"
                session.add(job)
                session.commit()
                await self.submit_job(job_id)
                return True

            # Ensure torrent client exists for re-finalization
            if not self.torrent_client:
                container = SessionContainer(session=session, client_session=self.http_session)
                mam_config_def = await MamIndexer.get_configurations(container)
                config = cast(ValuedMamConfigurations, create_valued_configuration(mam_config_def, session, check_required=False))
                client_type = config.download_client or "transmission"
                if client_type == "qbittorrent":
                    self.torrent_client = QbitClient(
                        self.http_session,
                        config.qbittorrent_url or "http://qbittorrent:8080",
                        config.qbittorrent_username or "",
                        config.qbittorrent_password or "",
                    )
                else:
                    self.torrent_client = TransmissionClient(
                        self.http_session,
                        config.transmission_url or "http://transmission:9091/transmission/rpc",
                        config.transmission_username,
                        config.transmission_password,
                    )

            if self.torrent_client:
                torrents = await self.torrent_client.get_torrents([job.transmission_hash])
                snapshot = torrents.get(job.transmission_hash) or next(iter(torrents.values()), {})
                if isinstance(self.torrent_client, QbitClient) and snapshot and not snapshot.get("files"):
                    try:
                        snapshot["files"] = await self.torrent_client.list_files(job.transmission_hash)
                    except Exception as exc:
                        logger.warning("DownloadManager: unable to fetch qBittorrent file list", error=str(exc))

            if snapshot:
                job.status = DownloadJobStatus.processing
                job.message = "Retrying post-processing"
                session.add(job)
                session.commit()
            else:
                # Torrent missing; requeue download
                job.status = DownloadJobStatus.pending
                job.message = "Retrying download (torrent missing)"
                session.add(job)
                session.commit()
                await self.submit_job(job_id)
                return True

        await self._finalize_job(job_id, snapshot)
        return True
