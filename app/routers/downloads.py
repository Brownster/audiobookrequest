from fastapi import APIRouter, Depends, Request, Security
from sqlalchemy import desc
from sqlmodel import Session, select

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import DownloadJob
from app.util.db import get_session
from app.util.templates import template_response


router = APIRouter()


def _serialize_job(job: DownloadJob) -> dict:
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
