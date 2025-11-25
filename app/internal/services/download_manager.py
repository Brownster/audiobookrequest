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
from sqlmodel import select, Session
from torf import Torrent as TorfTorrent

from app.util.log import logger
from app.util.db import open_session
from app.internal.models import DownloadJob, DownloadJobStatus, BookRequest
from app.internal.clients.mam import MamClientSettings, MyAnonamouseClient
from app.internal.clients.torrent.abstract import AbstractTorrentClient
from app.internal.clients.torrent.transmission import TransmissionClient
from app.internal.clients.torrent.qbittorrent import QbitClient
from app.internal.processing.postprocess import PostProcessor, PostProcessingError
from app.internal.services.seeding import build_seed_configuration, TorrentSeedConfiguration
from app.internal.env_settings import Settings
from app.internal.indexers.configuration import create_valued_configuration
from app.internal.indexers.mam import MamIndexer, ValuedMamConfigurations
from app.internal.indexers.abstract import SessionContainer

def _ensure_directory(path_str: str) -> Path:
    path = Path(path_str)
    path.mkdir(parents=True, exist_ok=True)
    return path

# We need to define a settings adapter or use the global settings
# For now, we'll assume some settings are available in env_settings or we default them.
# We might need to extend Settings in env_settings.py to include MAM/Torrent configs.

class DownloadManager:
    _instance: Optional[DownloadManager] = None

    def __init__(self):
        self.queue: asyncio.Queue[str] = asyncio.Queue() # Queue of Job IDs
        self.worker_task: Optional[asyncio.Task] = None
        self.monitor_task: Optional[asyncio.Task] = None
        self._stopping = False
        self.http_session: Optional[ClientSession] = None
        self._last_mam_retry: Optional[datetime] = None
        
        # We'll initialize these in start()
        self.mam_client: Optional[MyAnonamouseClient] = None
        self.torrent_client: Optional[AbstractTorrentClient] = None
        self.postprocessor: Optional[PostProcessor] = None
        
        # Settings placeholders (should be loaded from config)
        self.download_dir = Settings().app.download_dir if hasattr(Settings().app, "download_dir") else "/tmp/abr/audiobooks/"
        self.postprocess_tmp_dir = "/tmp/abr/mam-service"
        self.transmission_url = "http://transmission:9091/transmission/rpc"
        self.mam_session_id = ""

    @classmethod
    def get_instance(cls) -> DownloadManager:
        if cls._instance is None:
            cls._instance = DownloadManager()
        return cls._instance

    async def start(self):
        if self.worker_task:
            return

        self.http_session = ClientSession()
        
        # Ensure directories exist
        self.download_dir = str(_ensure_directory(self.download_dir))
        self.postprocess_tmp_dir = str(_ensure_directory(self.postprocess_tmp_dir))

        # Initialize PostProcessor
        self.postprocessor = PostProcessor(
            output_dir=Path(self.download_dir), # This should be the final destination
            tmp_dir=Path(self.postprocess_tmp_dir),
            http_session=self.http_session
        )

        # Preload settings and test torrent client connection if possible
        try:
            with open_session() as session:
                container = SessionContainer(session=session, client_session=self.http_session)
                mam_config_def = await MamIndexer.get_configurations(container)
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

            mam_session_id = config.mam_session_id
            
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
            
            # Init Torrent Client
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
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("DownloadManager: monitor failed", error=str(exc))

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

            mam_session_id = config.mam_session_id
            if not mam_session_id:
                return

            cutoff = now - retry_interval
            pending = session.exec(
                select(BookRequest).where(
                    BookRequest.downloaded == False,  # noqa: E712
                    BookRequest.mam_unavailable == True,  # noqa: E712
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

            jobs = session.exec(select(DownloadJob).where(DownloadJob.status.in_([DownloadJobStatus.downloading, DownloadJobStatus.seeding]))).all()
            
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
                
                # Update seed stats
                job.seed_seconds = job.seed_seconds or 0
                # Transmission reports secondsSeeding; qB uses seeding_time
                elapsed = t_info.get("seeding_time") or t_info.get("secondsSeeding") or 0
                if isinstance(elapsed, (int, float)):
                    job.seed_seconds = max(job.seed_seconds, int(elapsed))

                # Determine completion
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

                if is_downloaded and meets_seed_time and meets_ratio and job.status != DownloadJobStatus.processing:
                    job.status = DownloadJobStatus.processing
                    job.message = "Download complete, starting processing"
                    session.add(job)
                    session.commit()
                    
                    # Trigger finalization in background
                    asyncio.create_task(self._finalize_job(str(job.id), t_info))

    async def _finalize_job(self, job_id: str, torrent_snapshot: dict):
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
                if content_path:
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
                download_dir = local_prefix + str(download_dir)[len(remote_prefix) :]
                logger.info(
                    "DownloadManager: mapped remote path",
                    remote_prefix=remote_prefix,
                    local_prefix=local_prefix,
                    mapped_path=download_dir,
                )

            if download_dir:
                snapshot["downloadDir"] = download_dir

            # qBittorrent needs an extra call to get file list
            if not snapshot.get("files") and isinstance(self.torrent_client, QbitClient):
                try:
                    snapshot["files"] = await self.torrent_client.list_files(job.transmission_hash)
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
                destination = await self.postprocessor.process(str(job.id), request, snapshot)
                # Keep status as seeding to reflect ongoing seeding on private trackers
                job.status = DownloadJobStatus.seeding
                job.destination_path = str(destination)
                job.message = f"Processed -> {destination}"
                job.completed_at = datetime.utcnow()
                session.add(job)
                session.commit()
                
                # Cleanup torrent
                # await self.torrent_client.remove_torrent(job.transmission_hash)
                
            except Exception as exc:
                job.status = DownloadJobStatus.failed
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
