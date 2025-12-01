import asyncio
import uuid
from pathlib import Path
from dataclasses import dataclass
from fastapi import APIRouter, Depends, Request, Security, HTTPException
from fastapi import Form
from sqlalchemy import desc
from sqlmodel import Session, select
import os

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import DownloadJob, DownloadJobStatus, GroupEnum, MediaType, BookRequest
from app.internal.services.download_manager import DownloadManager
from app.internal.services.seeding import TorrentSeedConfiguration
from app.util.db import get_session
from app.util.templates import template_response


router = APIRouter()


@dataclass
class BookCandidate:
    root: Path
    title: str
    authors: list[str]


def _serialize_job(job: DownloadJob) -> dict:
    # Extract client state from message if it contains state information
    client_state = None
    if job.message:
        msg_lower = job.message.lower()
        if any(keyword in msg_lower for keyword in ["qb state:", "force-start", "resuming", "inactive"]):
            client_state = job.message

    # Calculate seed time remaining
    seed_time_remaining_seconds = None
    seed_time_elapsed_seconds = job.seed_seconds or 0
    seed_time_required_seconds = None
    if job.seed_configuration:
        config = TorrentSeedConfiguration.from_record(job.seed_configuration)
        if config and config.required_seed_seconds:
            seed_time_required_seconds = config.required_seed_seconds
            seed_time_remaining_seconds = max(0, config.required_seed_seconds - seed_time_elapsed_seconds)

    return {
        "id": str(job.id),
        "title": job.title,
        "status": job.status.value if hasattr(job.status, "value") else str(job.status),
        "message": job.message,
        "provider": job.provider,
        "torrent_id": job.torrent_id,
        "hash": job.transmission_hash,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
        "destination_path": job.destination_path,
        "media_type": getattr(job, "media_type", None),
        "client_state": client_state,
        "seed_time_elapsed": seed_time_elapsed_seconds,
        "seed_time_required": seed_time_required_seconds,
        "seed_time_remaining": seed_time_remaining_seconds,
    }


@router.get("/downloads")
async def downloads(
    request: Request,
    user: DetailedUser = Security(ABRAuth()),
):
    return template_response(
        "downloads.html",
        request,
        user,
        {},
    )


@router.get("/downloads/fragment")
async def downloads_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth()),
):
    from app.internal.models import DownloadJobStatus

    # Exclude completed jobs - those are shown in history
    jobs = session.exec(
        select(DownloadJob)
        .where(DownloadJob.status != DownloadJobStatus.completed)
        .order_by(desc(DownloadJob.created_at))
        .limit(100)
    ).all()
    serialized = [_serialize_job(j) for j in jobs]
    return template_response(
        "components/downloads_table.html",
        request,
        user,
        {"jobs": serialized},
    )


# Manual import (post-process existing files)
@router.get("/downloads/manual")
async def manual_import_form(
    request: Request,
    user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    browse_base = os.getenv("ABR_IMPORT_ROOT")
    return template_response(
        "downloads_manual_import.html",
        request,
        user,
        {"preview": None, "error": None, "browse_base": browse_base},
    )


def _parse_title_author_from_path(path: str) -> tuple[str, list[str]]:
    """Best-effort parse of `Title - Author` or `Author - Title`."""
    from pathlib import Path
    import re

    name = Path(path).stem
    parts = [p.strip() for p in re.split(r" - ", name, maxsplit=1)]
    if len(parts) == 2:
        # Heuristic: if second part contains a space, assume it's the author; otherwise leave as is
        title, author = parts
        if "," in title and " " not in author:
            # Sometimes reversed; fall back to original order
            pass
        return title or name, [author] if author else []
    return name, []


def _has_audio(path: Path) -> bool:
    for ext in AUDIO_EXTENSIONS:
        if any(path.rglob(f"*{ext}")):
            return True
    return False


def _discover_books(base: Path, multi: bool) -> list[BookCandidate]:
    """
    If multi=True, treat each immediate subfolder containing audio as a separate book.
    Otherwise treat the base as a single book.
    """
    candidates: list[BookCandidate] = []
    if multi:
        for child in sorted(p for p in base.iterdir() if p.is_dir()):
            if not _has_audio(child):
                continue
            title, authors = _parse_title_author_from_path(child.name)
            parent_author, _ = _parse_title_author_from_path(base.name)
            if parent_author and not authors:
                authors = [parent_author]
            candidates.append(BookCandidate(root=child, title=title, authors=authors))
        if candidates:
            return candidates

    # Fallback: single book at base
    title, authors = _parse_title_author_from_path(base.name)
    parent_author, _ = _parse_title_author_from_path(base.parent.name)
    if parent_author and not authors:
        authors = [parent_author]
    candidates.append(BookCandidate(root=base, title=title, authors=authors))
    return candidates


def _build_fake_snapshot_sync(source: Path) -> dict:
    """Build a torrent_snapshot-like dict for the post-processors."""
    if source.is_file():
        download_dir = source.parent
        files = [{"name": source.name}]
        name = source.name
    else:
        download_dir = source
        name = source.name
        files = []
        for p in source.rglob("*"):
            if p.is_file():
                rel = p.relative_to(download_dir).as_posix()
                files.append({"name": rel})
    return {
        "downloadDir": str(download_dir),
        "name": name,
        "files": files,
    }


async def _build_fake_snapshot(source: Path) -> dict:
    """Build a torrent_snapshot-like dict for the post-processors (async to avoid blocking)."""
    return await asyncio.to_thread(_build_fake_snapshot_sync, source)


@router.post("/downloads/manual/preview")
async def manual_import_preview(
    request: Request,
    source_path: str = Form(...),
    media_type: str = Form("audiobook"),
    multi_books: bool = Form(False),
    user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    import uuid
    import asyncio
    from datetime import datetime
    from pathlib import Path
    from aiohttp import ClientSession

    from app.internal.processing.postprocess import PostProcessor, EbookPostProcessor, PostProcessingError
    from app.internal.env_settings import Settings
    from app.util.log import logger

    path = Path(source_path).expanduser()
    error = None
    preview = None
    books: list[BookCandidate] = []
    if not await asyncio.to_thread(path.exists):
        error = f"Path does not exist: {path}"
    else:
        books = _discover_books(path, multi_books)
        first = books[0] if books else None
        preview = {
            "path": str(path),
            "media_type": media_type,
            "title": first.title if first else path.stem,
            "authors": ", ".join(first.authors) if first and first.authors else "",
            "multi": multi_books,
            "books": books,
        }

    return template_response(
        "downloads_manual_import.html",
        request,
        user,
        {"preview": preview, "error": error, "browse_base": os.getenv("ABR_IMPORT_ROOT")},
    )


@router.post("/downloads/manual/import")
async def manual_import_run(
    request: Request,
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
    source_path: str = Form(""),
    media_type: str = Form("audiobook"),
    multi_books: bool = Form(False),
    title: str = Form(""),
    authors: str = Form(""),
):
    import asyncio
    from datetime import datetime
    from aiohttp import ClientSession

    from app.internal.processing.postprocess import PostProcessor, EbookPostProcessor, PostProcessingError
    from app.internal.env_settings import Settings
    from app.util.log import logger

    logger.info("Manual import started", source_path=source_path, media_type=media_type, multi=multi_books)

    path = Path(source_path).expanduser()
    if not await asyncio.to_thread(path.exists):
        return template_response(
            "downloads_manual_import.html",
            request,
            user,
            {"preview": None, "error": f"Path does not exist: {path}", "browse_base": os.getenv("ABR_IMPORT_ROOT")},
        )

    books = _discover_books(path, multi_books)
    if not books:
        return template_response(
            "downloads_manual_import.html",
            request,
            user,
            {"preview": None, "error": "No audio files found.", "browse_base": os.getenv("ABR_IMPORT_ROOT")},
        )

    settings = Settings().app
    tmp_dir = Path("/tmp/abr/manual-import")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    media_enum = MediaType(media_type)
    successes: list[str] = []

    for candidate in books:
        parsed_title, parsed_authors = _parse_title_author_from_path(candidate.root.name)
        final_title = title.strip() or candidate.title or parsed_title or candidate.root.stem
        author_list = [a.strip() for a in authors.split(",") if a.strip()] or candidate.authors or parsed_authors or ["Unknown Author"]

        job_id = uuid.uuid4()
        book = BookRequest(
            asin=f"manual-{job_id}",
            title=final_title,
            subtitle=None,
            authors=author_list,
            narrators=[],
            cover_image=None,
            release_date=datetime.utcnow(),
            runtime_length_min=0,
            downloaded=True,
            media_type=media_enum,
        )

        snapshot = await _build_fake_snapshot(candidate.root)

        async with ClientSession() as http_session:
            try:
                if media_enum == MediaType.ebook:
                    processor = EbookPostProcessor(Path(settings.book_dir), tmp_dir, http_session)
                    dest = await asyncio.wait_for(processor.process(str(job_id), book, snapshot), timeout=300.0)
                else:
                    processor = PostProcessor(Path(settings.download_dir), tmp_dir, enable_merge=True, http_session=http_session)
                    dest = await asyncio.wait_for(processor.process(str(job_id), book, snapshot), timeout=300.0)
            except asyncio.TimeoutError:
                return template_response(
                    "downloads_manual_import.html",
                    request,
                    user,
                    {
                        "preview": {
                            "path": str(path),
                            "media_type": media_type,
                            "title": final_title,
                            "authors": ", ".join(author_list),
                            "multi": multi_books,
                            "books": books,
                        },
                        "error": "Post-processing timed out.",
                        "browse_base": os.getenv("ABR_IMPORT_ROOT"),
                    },
                )
            except PostProcessingError as exc:
                return template_response(
                    "downloads_manual_import.html",
                    request,
                    user,
                    {
                        "preview": {
                            "path": str(path),
                            "media_type": media_type,
                            "title": final_title,
                            "authors": ", ".join(author_list),
                            "multi": multi_books,
                            "books": books,
                        },
                        "error": f"Post-processing failed: {exc}",
                        "browse_base": os.getenv("ABR_IMPORT_ROOT"),
                    },
                )

        job = DownloadJob(
            id=job_id,
            request_id=None,
            media_type=media_enum,
            status=DownloadJobStatus.completed,
            title=final_title,
            provider="manual",
            torrent_id=None,
            transmission_hash=None,
            transmission_id=None,
            seed_configuration={},
            destination_path=str(dest),
            message="Imported manually",
            created_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
        )
        session.add(job)
        session.commit()
        successes.append(str(dest))

    return template_response(
        "downloads_manual_import.html",
        request,
        user,
        {
            "preview": {
                "path": str(path),
                "media_type": media_type,
                "title": title,
                "authors": authors,
                "multi": multi_books,
                "books": books,
            },
            "success": f"Imported {len(successes)} item(s)",
            "error": None,
            "browse_base": os.getenv("ABR_IMPORT_ROOT"),
        },
    )


@router.get("/downloads/manual/browse")
async def manual_import_browse(
    request: Request,
    session: Session = Depends(get_session),
    base: str | None = None,
    user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    # Determine base from param or known config
    base_path = Path(base).expanduser() if base else None
    if not base_path:
        # try qbit local prefix
        from app.internal.indexers.configuration import indexer_configuration_cache

        local_prefix = indexer_configuration_cache.get(session, "MyAnonamouse_qbittorrent_local_path_prefix")
        if local_prefix:
            base_path = Path(local_prefix).expanduser()
    if not base_path:
        return template_response(
            "components/manual_import_browser.html",
            request,
            user,
            {"entries": [], "base": None, "error": "No base path configured (set ABR_IMPORT_ROOT or qB Local Path Prefix)."},
        )

    entries: list[dict] = []
    try:
        with os.scandir(base_path) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir() or entry.is_file():
                    entries.append({"name": entry.name, "path": str(Path(base_path) / entry.name), "is_dir": entry.is_dir()})
                if len(entries) >= 50:
                    break
    except Exception as exc:
        return template_response(
            "components/manual_import_browser.html",
            request,
            user,
            {"entries": [], "base": str(base_path), "error": f"Browse failed: {exc}"},
        )

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return template_response(
        "components/manual_import_browser.html",
        request,
        user,
        {"entries": entries, "base": str(base_path), "error": None},
    )


@router.post("/downloads/{job_id}/delete")
async def delete_download(
    job_id: str,
    request: Request,
    from_history: bool = False,
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth()),
):
    from app.internal.models import DownloadJobStatus

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Download job not found")

    job = session.get(DownloadJob, job_uuid)
    if not job:
        raise HTTPException(status_code=404, detail="Download job not found")
    session.delete(job)
    session.commit()

    # Return appropriate table based on source
    if from_history:
        jobs = session.exec(
            select(DownloadJob)
            .where(DownloadJob.status == DownloadJobStatus.completed)
            .order_by(desc(DownloadJob.completed_at))
            .limit(200)
        ).all()
        template = "components/downloads_history_table.html"
    else:
        jobs = session.exec(
            select(DownloadJob)
            .where(DownloadJob.status != DownloadJobStatus.completed)
            .order_by(desc(DownloadJob.created_at))
            .limit(100)
        ).all()
        template = "components/downloads_table.html"

    serialized = [_serialize_job(j) for j in jobs]
    return template_response(
        template,
        request,
        user,
        {"jobs": serialized},
    )


@router.post("/downloads/{job_id}/reprocess")
async def reprocess_download(
    job_id: str,
    request: Request,
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth()),
):
    dm = DownloadManager.get_instance()
    await dm.reprocess_job(job_id)

    jobs = session.exec(
        select(DownloadJob).order_by(desc(DownloadJob.created_at)).limit(100)
    ).all()
    serialized = [_serialize_job(j) for j in jobs]
    return template_response(
        "components/downloads_table.html",
        request,
        user,
        {"jobs": serialized},
    )


@router.get("/downloads/history")
async def downloads_history(
    request: Request,
    user: DetailedUser = Security(ABRAuth()),
):
    return template_response(
        "downloads_history.html",
        request,
        user,
        {},
    )


@router.get("/downloads/history/fragment")
async def downloads_history_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth()),
):
    from app.internal.models import DownloadJobStatus

    # Show completed jobs that are no longer actively seeding
    jobs = session.exec(
        select(DownloadJob)
        .where(DownloadJob.status == DownloadJobStatus.completed)
        .order_by(desc(DownloadJob.completed_at))
        .limit(200)
    ).all()
    serialized = [_serialize_job(j) for j in jobs]
    return template_response(
        "components/downloads_history_table.html",
        request,
        user,
        {"jobs": serialized},
    )
