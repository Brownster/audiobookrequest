import uuid
from fastapi import APIRouter, Depends, Request, Security, HTTPException
from sqlalchemy import desc
from sqlmodel import Session, select

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import DownloadJob, DownloadJobStatus, GroupEnum, MediaType, BookRequest
from app.internal.services.download_manager import DownloadManager
from app.internal.services.seeding import TorrentSeedConfiguration
from app.util.db import get_session
from app.util.templates import template_response


router = APIRouter()


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
    return template_response(
        "downloads_manual_import.html",
        request,
        user,
        {"preview": None, "error": None},
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


def _build_fake_snapshot(source: Path) -> dict:
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


@router.post("/downloads/manual/preview")
async def manual_import_preview(
    request: Request,
    source_path: str,
    media_type: str = "audiobook",
    user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    from pathlib import Path

    path = Path(source_path).expanduser()
    error = None
    preview = None
    if not path.exists():
        error = f"Path does not exist: {path}"
    else:
        title, authors = _parse_title_author_from_path(path.name)
        preview = {
            "path": str(path),
            "media_type": media_type,
            "title": title,
            "authors": ", ".join(authors) if authors else "",
        }

    return template_response(
        "downloads_manual_import.html",
        request,
        user,
        {"preview": preview, "error": error},
    )


@router.post("/downloads/manual/import")
async def manual_import_run(
    request: Request,
    session: Session = Depends(get_session),
    user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
    source_path: str = "",
    media_type: str = "audiobook",
    title: str = "",
    authors: str = "",
):
    import uuid
    from datetime import datetime
    from pathlib import Path

    from app.internal.processing.postprocess import PostProcessor, EbookPostProcessor, PostProcessingError
    from app.internal.env_settings import Settings

    path = Path(source_path).expanduser()
    if not path.exists():
        return template_response(
            "downloads_manual_import.html",
            request,
            user,
            {"preview": None, "error": f"Path does not exist: {path}"},
        )

    parsed_title, parsed_authors = _parse_title_author_from_path(path.name)
    final_title = title.strip() or parsed_title or path.stem
    author_list = [a.strip() for a in authors.split(",") if a.strip()] or parsed_authors or ["Unknown Author"]

    job_id = uuid.uuid4()
    settings = Settings().app
    tmp_dir = Path("/tmp/abr/manual-import")
    media_enum = MediaType(media_type)

    # Build a lightweight BookRequest surrogate for tagging
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

    snapshot = _build_fake_snapshot(path)
    try:
        if media_enum == MediaType.ebook:
            processor = EbookPostProcessor(Path(settings.book_dir), tmp_dir, None)
            dest = await processor.process(str(job_id), book, snapshot)
        else:
            processor = PostProcessor(Path(settings.download_dir), tmp_dir, enable_merge=True, http_session=None)
            dest = await processor.process(str(job_id), book, snapshot)
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
                },
                "error": f"Post-processing failed: {exc}",
            },
        )

    # Record as a completed manual job
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
            },
            "success": f"Imported to {dest}",
            "error": None,
        },
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
