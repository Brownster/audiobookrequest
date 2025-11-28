import uuid
from datetime import datetime
from collections import defaultdict
from typing import Annotated, Literal, Optional

from aiohttp import ClientSession
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
    Security,
)
from pydantic import BaseModel
from sqlalchemy import func, desc
from sqlmodel import Session, asc, col, not_, select

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import (
    BookRequest,
    BookWishlistResult,
    EventEnum,
    GroupEnum,
    ManualBookRequest,
    DownloadJob,
    DownloadJobStatus,
    User,
)
from app.internal.clients.mam import MamClientSettings, MyAnonamouseClient
from app.internal.indexers.configuration import indexer_configuration_cache
from app.internal.notifications import (
    send_all_manual_notifications,
    send_all_notifications,
)
from app.internal.audiobookshelf.config import abs_config
from app.internal.audiobookshelf.client import abs_trigger_scan
from app.internal.query import query_sources
from app.internal.services.download_manager import DownloadManager
from app.util.connection import get_connection
from app.util.db import get_session, open_session
from app.util.redirect import BaseUrlRedirectResponse
from app.util.templates import template_response

router = APIRouter(prefix="/wishlist")


class WishlistCounts(BaseModel):
    requests: int
    downloaded: int
    manual: int


def get_wishlist_counts(
    session: Session, user: Optional[User] = None
) -> WishlistCounts:
    """Optional user limits results to only the current user if they are not an admin."""
    username = None if user is None or user.is_admin() else user.username

    downloaded = session.exec(
        select(func.count(func.distinct(BookRequest.asin)))
        .where(
            BookRequest.downloaded,
            not username or BookRequest.user_username == username,
            col(BookRequest.user_username).is_not(None),
        )
        .select_from(BookRequest)
    ).one()

    requests = session.exec(
        select(func.count(func.distinct(BookRequest.asin)))
        .where(
            not_(BookRequest.downloaded),
            not username or BookRequest.user_username == username,
            col(BookRequest.user_username).is_not(None),
        )
        .select_from(BookRequest)
    ).one()

    manual = session.exec(
        select(func.count())
        .select_from(ManualBookRequest)
        .where(
            not username or ManualBookRequest.user_username == username,
            col(ManualBookRequest.user_username).is_not(None),
        )
    ).one()

    return WishlistCounts(
        requests=requests,
        downloaded=downloaded,
        manual=manual,
    )


def get_wishlist_books(
    session: Session,
    username: Optional[str] = None,
    response_type: Literal["all", "downloaded", "not_downloaded"] = "all",
) -> list[BookWishlistResult]:
    """
    Gets the books that have been requested. If a username is given only the books requested by that
    user are returned. If no username is given, all book requests are returned.
    """
    book_requests = session.exec(
        select(BookRequest).where(
            not username or BookRequest.user_username == username,
            col(BookRequest.user_username).is_not(None),
        )
    ).all()

    request_ids = [b.id for b in book_requests if b.id]
    jobs: dict[uuid.UUID, DownloadJob] = {}
    active_job_request_ids: set[uuid.UUID] = set()
    if request_ids:
        all_jobs = session.exec(
            select(DownloadJob)
            .where(col(DownloadJob.request_id).in_(request_ids))
            .order_by(desc(DownloadJob.created_at))
        ).all()
        seen: set[uuid.UUID] = set()
        for j in all_jobs:
            if j.request_id in seen:
                continue
            jobs[j.request_id] = j
            seen.add(j.request_id)
            # Track requests with active downloads (seeding, processing, completed)
            # These should be hidden from wishlist and tracked on downloads page instead
            if j.status in [
                DownloadJobStatus.seeding,
                DownloadJobStatus.processing,
                DownloadJobStatus.completed,
            ]:
                active_job_request_ids.add(j.request_id)

    # group by asin and aggregate all usernames
    usernames: dict[str, list[str]] = defaultdict(list)
    distinct_books: dict[str, BookRequest] = {}
    for book in book_requests:
        if book.asin not in distinct_books:
            distinct_books[book.asin] = book
        if book.user_username:
            usernames[book.asin].append(book.user_username)

    # add information of what users requested the book
    books: list[BookWishlistResult] = []
    downloaded: list[BookWishlistResult] = []
    for asin, book in distinct_books.items():
        # Skip books with active download jobs (they're being handled)
        if book.id in active_job_request_ids and not book.downloaded:
            continue

        b = BookWishlistResult.model_validate(book)
        b.requested_by = usernames[asin]
        b.mam_unavailable = getattr(book, "mam_unavailable", False)
        job = jobs.get(book.id) if hasattr(book, "id") else None
        if b.downloaded:
            b.pipeline_status = "completed"
            b.pipeline_message = "Delivered to library"
        elif job:
            b.pipeline_status = job.status.value if hasattr(job.status, "value") else str(job.status)
            b.pipeline_message = job.message or ""
        elif b.mam_unavailable and book.mam_last_check:
            b.pipeline_status = "no_results"
            b.pipeline_message = f"No MAM results (last check {book.mam_last_check:%Y-%m-%d})"
        else:
            b.pipeline_status = "pending"
            b.pipeline_message = "Awaiting MAM search/queue"
        if b.downloaded:
            downloaded.append(b)
        else:
            books.append(b)

    if response_type == "downloaded":
        return downloaded
    if response_type == "not_downloaded":
        return books
    return books + downloaded


@router.get("")
async def wishlist(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    user: DetailedUser = Security(ABRAuth()),
):
    username = None if user.is_admin() else user.username
    books = get_wishlist_books(session, username, "not_downloaded")
    counts = get_wishlist_counts(session, user)
    return template_response(
        "wishlist_page/wishlist.html",
        request,
        user,
        {"books": books, "page": "wishlist", "counts": counts},
    )


@router.get("/downloaded")
async def downloaded(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    user: DetailedUser = Security(ABRAuth()),
):
    username = None if user.is_admin() else user.username
    books = get_wishlist_books(session, username, "downloaded")
    counts = get_wishlist_counts(session, user)
    return template_response(
        "wishlist_page/wishlist.html",
        request,
        user,
        {"books": books, "page": "downloaded", "counts": counts},
    )


@router.patch("/downloaded/{asin}")
async def update_downloaded(
    request: Request,
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    client_session: Annotated[ClientSession, Depends(get_connection)],
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    books = session.exec(
        select(BookRequest, User)
        .join(User, isouter=True)
        .where(BookRequest.asin == asin)
    ).all()

    requested_by = [User.model_validate(user) for [_, user] in books if user]

    for [book, _] in books:
        book.downloaded = True
        session.add(book)
    session.commit()

    if len(requested_by) > 0:
        background_task.add_task(
            send_all_notifications,
            event_type=EventEnum.on_successful_download,
            requester=requested_by[0],  # TODO: support multiple requesters
            book_asin=asin,
        )

    # Trigger ABS library scan in background if configured
    if abs_config.is_valid(session):
        background_task.add_task(abs_trigger_scan, session, client_session)

    username = None if admin_user.is_admin() else admin_user.username
    books = get_wishlist_books(session, username, "not_downloaded")
    counts = get_wishlist_counts(session, admin_user)

    return template_response(
        "wishlist_page/wishlist.html",
        request,
        admin_user,
        {
            "books": books,
            "page": "wishlist",
            "counts": counts,
            "update_tablist": True,
        },
        block_name="book_wishlist",
    )


def _get_all_manual_requests(session: Session, user: User):
    return session.exec(
        select(ManualBookRequest)
        .where(
            user.is_admin() or ManualBookRequest.user_username == user.username,
            col(ManualBookRequest.user_username).is_not(None),
        )
        .order_by(asc(ManualBookRequest.downloaded))
    ).all()


@router.get("/manual")
async def manual(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    user: DetailedUser = Security(ABRAuth()),
):
    books = _get_all_manual_requests(session, user)
    counts = get_wishlist_counts(session, user)
    return template_response(
        "wishlist_page/manual.html",
        request,
        user,
        {"books": books, "page": "manual", "counts": counts},
    )


@router.patch("/manual/{id}")
async def downloaded_manual(
    request: Request,
    id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    client_session: Annotated[ClientSession, Depends(get_connection)],
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    book_request = session.get(ManualBookRequest, id)
    if book_request:
        book_request.downloaded = True
        session.add(book_request)
        session.commit()

        background_task.add_task(
            send_all_manual_notifications,
            event_type=EventEnum.on_successful_download,
            book_request=ManualBookRequest.model_validate(book_request),
        )

        # Trigger ABS library scan in background if configured
        if abs_config.is_valid(session):
            background_task.add_task(abs_trigger_scan, session, client_session)

    books = _get_all_manual_requests(session, admin_user)
    counts = get_wishlist_counts(session, admin_user)

    return template_response(
        "wishlist_page/manual.html",
        request,
        admin_user,
        {
            "books": books,
            "page": "manual",
            "counts": counts,
            "update_tablist": True,
        },
        block_name="book_wishlist",
    )


@router.delete("/manual/{id}")
async def delete_manual(
    request: Request,
    id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    book = session.get(ManualBookRequest, id)
    if book:
        session.delete(book)
        session.commit()

    books = _get_all_manual_requests(session, admin_user)
    counts = get_wishlist_counts(session, admin_user)

    return template_response(
        "wishlist_page/manual.html",
        request,
        admin_user,
        {
            "books": books,
            "page": "manual",
            "counts": counts,
            "update_tablist": True,
        },
        block_name="book_wishlist",
    )


@router.post("/refresh/{asin}")
async def refresh_source(
    asin: str,
    background_task: BackgroundTasks,
    force_refresh: bool = False,
    user: DetailedUser = Security(ABRAuth()),
):
    # causes the sources to be placed into cache once they're done
    with open_session() as session:
        async with ClientSession() as client_session:
            background_task.add_task(
                query_sources,
                asin=asin,
                session=session,
                client_session=client_session,
                force_refresh=force_refresh,
                requester=User.model_validate(user),
            )
    return Response(status_code=202)


@router.get("/sources/{asin}")
async def list_sources(
    request: Request,
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    only_body: bool = False,
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    result = await query_sources(
        asin,
        session=session,
        client_session=client_session,
        requester=admin_user,
        only_return_if_cached=not only_body,  # on initial load we want to respond quickly
    )

    if only_body:
        return template_response(
            "wishlist_page/sources.html",
            request,
            admin_user,
            {"result": result},
            block_name="body",
        )
    return template_response(
        "wishlist_page/sources.html",
        request,
        admin_user,
        {"result": result},
    )


@router.post("/sources/{asin}")
async def download_book(
    asin: str,
    guid: Annotated[str, Form()],
    indexer_id: Annotated[int, Form()],
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    raise HTTPException(
        status_code=400, detail="Indexer downloads are disabled (Prowlarr removed)."
    )


@router.post("/auto-download/{asin}")
async def start_auto_download(
    request: Request,
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: DetailedUser = Security(ABRAuth(GroupEnum.trusted)),
):
    download_error: Optional[str] = "Indexer downloads are disabled."
    # Still warm any caches the lightweight query_sources stub might use (no-op today).
    await query_sources(
        asin=asin,
        start_auto_download=True,
        session=session,
        client_session=client_session,
        requester=user,
    )

    username = None if user.is_admin() else user.username
    books = get_wishlist_books(session, username)
    if download_error:
        errored_book = [b for b in books if b.asin == asin][0]
        errored_book.download_error = download_error

    counts = get_wishlist_counts(session, user)

    return template_response(
        "wishlist_page/wishlist.html",
        request,
        user,
        {
            "books": books,
            "page": "wishlist",
            "counts": counts,
            "update_tablist": True,
        },
        block_name="book_wishlist",
    )


@router.post("/mam-auto/{request_id}")
async def auto_download_mam(
    request: Request,
    request_id: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    user: DetailedUser = Security(ABRAuth(GroupEnum.trusted)),
):
    def _render(toast_error: str | None = None, toast_info: str | None = None, toast_success: str | None = None):
        books = get_wishlist_books(session, None if user.is_admin() else user.username, "not_downloaded")
        counts = get_wishlist_counts(session, user)
        return template_response(
            "wishlist_page/wishlist.html",
            request,
            user,
            {
                "books": books,
                "page": "wishlist",
                "counts": counts,
                "update_tablist": True,
                **({"toast_error": toast_error} if toast_error else {}),
                **({"toast_info": toast_info} if toast_info else {}),
                **({"toast_success": toast_success} if toast_success else {}),
            },
            block_name="book_wishlist",
        )

    try:
        req_uuid = uuid.UUID(request_id)
    except Exception:
        return _render(toast_error="Invalid request id")

    book_request = session.get(BookRequest, req_uuid)
    if not book_request:
        return _render(toast_error="Request not found")

    mam_session_id = indexer_configuration_cache.get(session, "MyAnonamouse_mam_session_id")
    if not mam_session_id:
        return _render(toast_error="MAM session not configured")

    settings = MamClientSettings(mam_session_id=mam_session_id)
    mam_client = MyAnonamouseClient(client_session, settings)

    # Use title (and author if present) to search MAM
    query = book_request.title
    if book_request.authors:
        query = f"{book_request.title} {', '.join(book_request.authors)}"

    results = await mam_client.search(query, limit=40)
    if not results:
        book_request.mam_unavailable = True
        book_request.mam_last_check = datetime.utcnow()
        session.add(book_request)
        session.commit()
        return _render(toast_error="No MAM results found")

    # Pick the best seeded result
    best = max(results, key=lambda r: (r.seeders, r.peers, -r.size))
    torrent_id = str(best.raw.get("id") or best.guid.split("-")[-1])
    if not torrent_id:
        book_request.mam_unavailable = True
        book_request.mam_last_check = datetime.utcnow()
        session.add(book_request)
        session.commit()
        return _render(toast_error="MAM result missing torrent id")

    # Avoid duplicate active jobs
    existing_job = session.exec(
        select(BookRequest, DownloadJob)
        .join(DownloadJob, DownloadJob.request_id == BookRequest.id, isouter=True)
        .where(
            BookRequest.id == book_request.id,
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
        return _render(toast_info="Already queued/downloading via MAM.")

    job = DownloadJob(
        request_id=book_request.id,
        title=best.title or book_request.title,
        torrent_id=torrent_id,
        status=DownloadJobStatus.pending,
        provider="qbittorrent",
        message="Queued via MAM auto-download",
    )
    session.add(job)
    book_request.mam_unavailable = False
    book_request.mam_last_check = datetime.utcnow()
    session.add(book_request)
    session.commit()

    await DownloadManager.get_instance().submit_job(str(job.id))

    return _render(toast_success="MAM download queued")
