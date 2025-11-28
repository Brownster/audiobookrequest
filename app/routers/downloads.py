import uuid
from fastapi import APIRouter, Depends, Request, Security, HTTPException
from sqlalchemy import desc
from sqlmodel import Session, select

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import DownloadJob
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
