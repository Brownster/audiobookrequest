import uuid
import json
from datetime import datetime
from typing import Annotated, Optional
from urllib.parse import quote_plus

from aiohttp import ClientSession
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Response,
    Query,
    Request,
    Security,
    status,
)
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.internal import book_search
from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.book_search import (
    audible_region_type,
    audible_regions,
    clear_old_book_caches,
    get_book_by_asin,
    get_region_from_settings,
    list_audible_books,
)
from app.internal.models import (
    BookRequest,
    BookSearchResult,
    EventEnum,
    GroupEnum,
    ManualBookRequest,
    User,
)
from app.internal.notifications import (
    send_all_manual_notifications,
    send_all_notifications,
)
from app.internal.query import query_sources
from app.internal.ranking.quality import quality_config
from app.routers.wishlist import get_wishlist_books, get_wishlist_counts
from app.util.connection import get_connection
from app.util.db import get_session, open_session
from app.util.recommendations import get_homepage_recommendations
from app.util.templates import template_response
from app.internal.clients.mam import MyAnonamouseClient, MamClientSettings
from app.internal.services.download_manager import DownloadManager
from app.internal.models import DownloadJob, DownloadJobStatus, MediaType
from app.internal.env_settings import Settings
from app.internal.indexers.configuration import indexer_configuration_cache
from app.util.log import logger
from app.util.redirect import BaseUrlRedirectResponse
from app.internal.audiobookshelf.config import abs_config
from app.internal.audiobookshelf.client import abs_book_exists
from app.internal.mam_normalizer import normalize_mam_results
from app.internal.clients.mam_categories import CATEGORY_MAPPINGS

router = APIRouter(prefix="/search")


def get_already_requested(session: Session, results: list[BookRequest], username: str):
    books: list[BookSearchResult] = []
    if len(results) > 0:
        # check what books are already requested by the user
        asins = {book.asin for book in results}
        requested_books = set(
            session.exec(
                select(BookRequest.asin).where(
                    col(BookRequest.asin).in_(asins),
                    BookRequest.user_username == username,
                )
            ).all()
        )

        for book in results:
            book_search = BookSearchResult.model_validate(book)
            if book.asin in requested_books:
                book_search.already_requested = True
            books.append(book_search)
    return books


@router.get("")
async def read_search(
    request: Request,
    client_session: Annotated[ClientSession, Depends(get_connection)],
    session: Annotated[Session, Depends(get_session)],
    query: Annotated[Optional[str], Query(alias="q")] = None,
    num_results: int = 20,
    page: int = 0,
    region: audible_region_type = get_region_from_settings(),
    user: DetailedUser = Security(ABRAuth()),
):
    if audible_regions.get(region) is None:
        raise HTTPException(status_code=400, detail="Invalid region")
    if query:
        results = await list_audible_books(
            session=session,
            client_session=client_session,
            query=query,
            num_results=num_results,
            page=page,
            audible_region=region,
        )
    else:
        results = []

    books: list[BookSearchResult] = []
    if len(results) > 0:
        books = get_already_requested(session, results, user.username)

    prowlarr_configured = False

    clear_old_book_caches(session)
    
    # Get recommendations if no search term is provided
    recommendations = None
    if not query:
        recommendations = get_homepage_recommendations(session, user)

    return template_response(
        "search.html",
        request,
        user,
        {
            "search_term": query or "",
            "search_results": books,
            "regions": audible_regions,
            "selected_region": region,
            "page": page,
            "auto_start_download": quality_config.get_auto_download(session)
            and user.is_above(GroupEnum.trusted),
            "prowlarr_configured": prowlarr_configured,
            "recommendations": recommendations,
        },
    )


@router.get("/suggestions")
async def search_suggestions(
    request: Request,
    query: Annotated[str, Query(alias="q")],
    user: DetailedUser = Security(ABRAuth()),
    region: audible_region_type = get_region_from_settings(),
):
    async with ClientSession() as client_session:
        suggestions = await book_search.get_search_suggestions(
            client_session, query, region
        )
        return template_response(
            "search.html",
            request,
            user,
            {"suggestions": suggestions},
            block_name="search_suggestions",
        )


async def background_start_query(asin: str, requester: User, auto_download: bool):
    with open_session() as session:
        async with ClientSession() as client_session:
            await query_sources(
                asin=asin,
                session=session,
                client_session=client_session,
                start_auto_download=auto_download,
                requester=requester,
            )


@router.post("/request/{asin}")
async def add_request(
    request: Request,
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    background_task: BackgroundTasks,
    query: Annotated[Optional[str], Form()] = None,
    page: Annotated[int, Form()] = 0,
    region: Annotated[audible_region_type, Form()] = get_region_from_settings(),
    num_results: Annotated[int, Form()] = 20,
    redirect_to_home: Annotated[Optional[str], Form()] = None,
    media_type: Annotated[str | None, Form()] = None,
    user: DetailedUser = Security(ABRAuth()),
):
    book = await get_book_by_asin(client_session, asin, region)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    try:
        chosen_media = MediaType(media_type) if media_type else MediaType.audiobook
    except Exception:
        chosen_media = MediaType.audiobook
    book.media_type = chosen_media

    # Check if already requested by anyone
    existing_any = session.exec(
        select(BookRequest).where(
            BookRequest.asin == asin,
            col(BookRequest.user_username).is_not(None),
        )
    ).first()
    if existing_any:
        if media_type and hasattr(existing_any, "media_type"):
            try:
                desired_media = MediaType(media_type)
                if existing_any.media_type != desired_media:
                    existing_any.media_type = desired_media
                    session.add(existing_any)
                    session.commit()
            except Exception:
                pass
        # If downloaded or has active job, short-circuit with toast
        if existing_any.downloaded:
            return template_response(
                "base.html",
                request,
                user,
                {"toast_info": "Already in requests/downloaded."},
                block_name="toast_block",
                headers={"HX-Retarget": "#toast-block"},
            )
        # Avoid duplicate request rows
        return template_response(
            "base.html",
            request,
            user,
            {"toast_info": "Already requested; check wishlist."},
            block_name="toast_block",
            headers={"HX-Retarget": "#toast-block"},
        )

    # Check if already in Audiobookshelf
    try:
        if abs_config.is_valid(session) and await abs_book_exists(session, client_session, book):
            return template_response(
                "base.html",
                request,
                user,
                {"toast_info": "Already in your library (Audiobookshelf)."},
                block_name="toast_block",
                headers={"HX-Retarget": "#toast-block"},
            )
    except Exception as e:
        logger.debug("ABS check skipped", error=str(e))

    book.user_username = user.username
    try:
        session.add(book)
        session.commit()
        # mark as awaiting MAM until processed
        book.mam_unavailable = True
        session.add(book)
        session.commit()
    except IntegrityError:
        session.rollback()
        pass  # ignore if already exists

    background_task.add_task(
        send_all_notifications,
        event_type=EventEnum.on_new_request,
        requester=User.model_validate(user),
        book_asin=asin,
    )

    if quality_config.get_auto_download(session) and user.is_above(GroupEnum.trusted):
        # start querying and downloading if auto download is enabled
        background_task.add_task(
            background_start_query,
            asin=asin,
            requester=User.model_validate(user),
            auto_download=True,
        )

    # If redirect_to_home is set, redirect to homepage instead of refreshing search results
    if redirect_to_home:
        recommendations = get_homepage_recommendations(session, user)
        return template_response(
            "root.html",
            request,
            user,
            {
                "recommendations": recommendations,
            },
        )

    if audible_regions.get(region) is None:
        raise HTTPException(status_code=400, detail="Invalid region")
    if query:
        results = await list_audible_books(
            session=session,
            client_session=client_session,
            query=query,
            num_results=num_results,
            page=page,
            audible_region=region,
        )
    else:
        results = []

    books: list[BookSearchResult] = []
    if len(results) > 0:
        books = get_already_requested(session, results, user.username)

    prowlarr_configured = False
    
    # Get recommendations if no search term is provided
    recommendations = None
    if not query:
        recommendations = get_homepage_recommendations(session, user)

    return template_response(
        "search.html",
        request,
        user,
        {
            "search_term": query or "",
            "search_results": books,
            "regions": audible_regions,
            "selected_region": region,
            "page": page,
            "auto_start_download": quality_config.get_auto_download(session)
            and user.is_above(GroupEnum.trusted),
            "prowlarr_configured": prowlarr_configured,
            "recommendations": recommendations,
        },
        block_name="book_results",
    )


@router.delete("/request/{asin}")
async def delete_request(
    request: Request,
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    downloaded: Optional[bool] = None,
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    books = session.exec(select(BookRequest).where(BookRequest.asin == asin)).all()
    if books:
        [session.delete(b) for b in books]
        session.commit()

    books = get_wishlist_books(
        session, None, "downloaded" if downloaded else "not_downloaded"
    )
    counts = get_wishlist_counts(session, admin_user)

    return template_response(
        "wishlist_page/wishlist.html",
        request,
        admin_user,
        {
            "books": books,
            "page": "downloaded" if downloaded else "wishlist",
            "counts": counts,
            "update_tablist": True,
        },
        block_name="book_wishlist",
    )


@router.get("/manual")
async def read_manual(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    id: Optional[uuid.UUID] = None,
    user: DetailedUser = Security(ABRAuth()),
):
    book = None
    if id:
        book = session.get(ManualBookRequest, id)

    auto_download = quality_config.get_auto_download(session)
    return template_response(
        "manual.html", request, user, {"auto_download": auto_download, "book": book}
    )


@router.post("/manual")
async def add_manual(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    background_task: BackgroundTasks,
    title: Annotated[str, Form()],
    author: Annotated[str, Form()],
    narrator: Annotated[Optional[str], Form()] = None,
    subtitle: Annotated[Optional[str], Form()] = None,
    publish_date: Annotated[Optional[str], Form()] = None,
    info: Annotated[Optional[str], Form()] = None,
    id: Optional[uuid.UUID] = None,
    user: DetailedUser = Security(ABRAuth()),
):
    if id:
        book_request = session.get(ManualBookRequest, id)
        if not book_request:
            raise HTTPException(status_code=404, detail="Book request not found")
        book_request.title = title
        book_request.subtitle = subtitle
        book_request.authors = author.split(",")
        book_request.narrators = narrator.split(",") if narrator else []
        book_request.publish_date = publish_date
        book_request.additional_info = info
    else:
        book_request = ManualBookRequest(
            user_username=user.username,
            title=title,
            authors=author.split(","),
            narrators=narrator.split(",") if narrator else [],
            subtitle=subtitle,
            publish_date=publish_date,
            additional_info=info,
        )
    session.add(book_request)
    session.commit()

    background_task.add_task(
        send_all_manual_notifications,
        event_type=EventEnum.on_new_request,
        book_request=ManualBookRequest.model_validate(book_request),
    )

    auto_download = quality_config.get_auto_download(session)

    return template_response(
        "manual.html",
        request,
        user,
        {"success": "Successfully added request", "auto_download": auto_download},
        block_name="form",
    )


@router.post("/request/{asin}/mam")
async def add_request_and_open_mam(
    request: Request,
    asin: str,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    region: Annotated[audible_region_type, Form()] = get_region_from_settings(),
    media_type: Annotated[str | None, Form()] = None,
    user: DetailedUser = Security(ABRAuth()),
):
    """Create (or reuse) a BookRequest and redirect to MAM search with request_id."""
    if audible_regions.get(region) is None:
        raise HTTPException(status_code=400, detail="Invalid region")

    def _redirect_with_hx(url: str):
        base = BaseUrlRedirectResponse(url)
        target = base.headers.get("location", url)
        # HTMX drops redirect headers after following 3xx; send HX-Redirect directly.
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"HX-Redirect": target})

    existing_request = session.exec(
        select(BookRequest).where(
            BookRequest.asin == asin, BookRequest.user_username == user.username
        )
    ).first()

    try:
        chosen_media = MediaType(media_type) if media_type else MediaType.audiobook
    except Exception:
        chosen_media = MediaType.audiobook

    # If any user already requested this ASIN, reuse that to avoid duplicates
    any_request = existing_request or session.exec(
        select(BookRequest).where(
            BookRequest.asin == asin, col(BookRequest.user_username).is_not(None)
        )
    ).first()
    if any_request:
        if existing_request and existing_request.media_type != chosen_media:
            existing_request.media_type = chosen_media
            session.add(existing_request)
            session.commit()
        return _redirect_with_hx(
            f"/search/mam?q={quote_plus(any_request.title)}&request_id={any_request.id}&media={chosen_media.value}"
        )

    book_request = existing_request
    if not existing_request:
        book = await get_book_by_asin(client_session, asin, region)
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")

        # If any user already requested this ASIN, reuse to avoid duplicates
        any_request = session.exec(
            select(BookRequest).where(
                BookRequest.asin == asin, col(BookRequest.user_username).is_not(None)
            )
        ).first()
        if any_request:
            return _redirect_with_hx(
                f"/search/mam?q={quote_plus(any_request.title)}&request_id={any_request.id}&media={chosen_media.value}"
            )

        # Check if already in Audiobookshelf
        try:
            if abs_config.is_valid(session) and await abs_book_exists(session, client_session, book):
                return _redirect_with_hx(f"/search?q={quote_plus(book.title)}")
        except Exception as e:
            logger.debug("ABS check skipped", error=str(e))

        book.user_username = user.username
        book.media_type = chosen_media
        try:
            session.add(book)
            session.commit()
            book_request = book
        except IntegrityError:
            session.rollback()
            book_request = session.exec(
                select(BookRequest).where(
                    BookRequest.asin == asin, BookRequest.user_username == user.username
                )
            ).first()

    if not book_request:
        raise HTTPException(status_code=500, detail="Failed to create request")

    return _redirect_with_hx(
        f"/search/mam?q={quote_plus(book_request.title)}&request_id={book_request.id}&media={chosen_media.value}"
    )


@router.get("/mam")
async def read_mam_search(
    request: Request,
    client_session: Annotated[ClientSession, Depends(get_connection)],
    session: Annotated[Session, Depends(get_session)],
    query: Annotated[Optional[str], Query(alias="q")] = None,
    request_id: Annotated[Optional[uuid.UUID], Query()] = None,
    media_type: Annotated[MediaType | None, Query(alias="media")] = MediaType.audiobook,
    user: DetailedUser = Security(ABRAuth()),
):
    results = []
    if query:
        mam_session_id = indexer_configuration_cache.get(session, "MyAnonamouse_mam_session_id")
        
        # Check for mock override
        use_mock = False
        if request.query_params.get("mock") == "1":
             use_mock = True
             if not mam_session_id:
                 mam_session_id = "mock_session_id"

        if not mam_session_id:
             return template_response(
                "mam_search.html",
                request,
                user,
                {
                    "search_term": query or "",
                    "search_results": [],
                    "error": "MAM Session ID not configured.",
                    "media_type": (media_type or MediaType.audiobook).value,
                },
            )

        settings = MamClientSettings(
            mam_session_id=mam_session_id,
            use_mock_data=use_mock,
        )

        client = MyAnonamouseClient(client_session, settings)
        try:
            if media_type == MediaType.ebook:
                ebook_cats = [c.tracker_id for c in CATEGORY_MAPPINGS if c.name.startswith("Ebooks")]
                categories = ebook_cats or [14]
            else:
                categories = [13]
            raw_results = await client.search(query, categories=categories)
            results = normalize_mam_results(raw_results)
        except Exception as e:
            logger.error("MAM search failed", error=str(e))
            return template_response(
                "mam_search.html",
                request,
                user,
                {
                    "search_term": query or "",
                    "search_results": [],
                    "error": f"Search failed: {e}",
                    "media_type": (media_type or MediaType.audiobook).value,
                },
            )

    return template_response(
        "mam_search.html",
        request,
        user,
        {
            "search_term": query or "",
            "search_results": results,
            "request_id": request_id,
            "media_type": (media_type or MediaType.audiobook).value,
        },
    )


@router.post("/mam/download")
async def download_mam(
    request: Request,
    torrent_id: Annotated[str, Form()],
    title: Annotated[str, Form()],
    session: Annotated[Session, Depends(get_session)],
    user: DetailedUser = Security(ABRAuth()),
    request_id: Annotated[str | None, Form()] = None,
    media_type: Annotated[str | None, Form()] = None,
    authors: Annotated[str | None, Form()] = None,
    cover_image: Annotated[str | None, Form()] = None,
):
    job_media_raw = media_type or MediaType.audiobook.value
    try:
        job_media_type = MediaType(job_media_raw)
    except Exception:
        job_media_type = MediaType.audiobook

    parsed_authors: list[str] = []
    if authors:
        try:
            parsed = json.loads(authors)
            if isinstance(parsed, list):
                parsed_authors = [str(a) for a in parsed if isinstance(a, str)]
        except Exception:
            parsed_authors = []
    book_request = None
    if request_id:
        try:
            request_uuid = uuid.UUID(request_id)
            book_request = session.get(BookRequest, request_uuid)
        except ValueError:
            book_request = None
    # If no request provided, create a lightweight request so the job can proceed
    if not book_request:
        book_request = BookRequest(
            asin=f"mam-{torrent_id}",
            title=title,
            subtitle=None,
            authors=parsed_authors or ["Unknown Author"],
            narrators=[],
            cover_image=cover_image,
            release_date=datetime.utcnow(),
            runtime_length_min=0,
            user_username=user.username,
            media_type=job_media_type,
        )
        session.add(book_request)
        session.commit()

    # Prevent duplicate jobs for the same request/torrent in active states
    existing_job = session.exec(
        select(DownloadJob).where(
            DownloadJob.request_id == book_request.id,
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
        return template_response(
            "base.html",
            request,
            user,
            {"toast_info": "Already queued/downloading."},
            block_name="toast_block",
            headers={"HX-Retarget": "#toast-block"},
        )

    # Create DownloadJob
    job = DownloadJob(
        request_id=book_request.id,
        title=title,
        torrent_id=torrent_id,
        status=DownloadJobStatus.pending,
        message="Queued for download",
        media_type=job_media_type,
    )
    session.add(job)
    session.commit()
    
    # Submit to DownloadManager
    await DownloadManager.get_instance().submit_job(str(job.id))
    
    return template_response(
        "base.html", # Just return a button or success message
        request,
        user,
        {"toast_success": "Download queued"},
        block_name="toast_block",
        headers={"HX-Retarget": "#toast-block"}
    )


def _parse_publish_date(date_str: str | None):
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _build_mam_sections(results: list, limit: int = 15):
    def is_free(r):
        flags = getattr(r, "flags", []) or []
        return any(f in {"free", "freeleech", "personal_freeleech"} for f in flags)

    def sort_date(r):
        dt = _parse_publish_date(getattr(r, "publish_date", None))
        return dt or datetime.min

    new = sorted(results, key=sort_date, reverse=True)[:limit]
    popular = sorted(results, key=lambda r: getattr(r, "seeders", 0), reverse=True)[:limit]
    freeleech = [r for r in results if is_free(r)][:limit]
    return {"new": new, "popular": popular, "freeleech": freeleech}


@router.get("/browse/mam")
async def browse_mam(
    request: Request,
    client_session: Annotated[ClientSession, Depends(get_connection)],
    session: Annotated[Session, Depends(get_session)],
    request_id: Annotated[Optional[str], Query()] = None,
    q: Annotated[Optional[str], Query()] = None,
    category: Annotated[Optional[int], Query()] = None,
    user: DetailedUser = Security(ABRAuth()),
):
    mam_session_id = indexer_configuration_cache.get(session, "MyAnonamouse_mam_session_id")
    
    # Allow optional mock mode via ?mock=1
    use_mock = request.query_params.get("mock") == "1"
    if use_mock and not mam_session_id:
        mam_session_id = "mock_session_id"

    if not mam_session_id:
        return template_response(
            "browse_mam.html",
            request,
            user,
            {
                "search_term": q or "",
                "sections": {"new": [], "popular": [], "freeleech": []},
                "request_id": request_id,
                "error": "MAM Session ID not configured.",
            },
        )

    settings = MamClientSettings(mam_session_id=mam_session_id, use_mock_data=use_mock)
    client = MyAnonamouseClient(client_session, settings)
    seed_query = q.strip() if q else "the"

    try:
        results = normalize_mam_results(await client.search(seed_query, limit=100, categories=[category] if category else None))
    except Exception as e:
        logger.error("MAM browse failed", error=str(e))
        return template_response(
            "browse_mam.html",
            request,
            user,
            {
                "search_term": q or "",
                "sections": {"new": [], "popular": [], "freeleech": []},
                "request_id": request_id,
                "error": f"Browse failed: {e}",
            },
        )

    sections = _build_mam_sections(results)
    audio_categories = [c for c in CATEGORY_MAPPINGS if c.name.startswith("Audiobooks")]
    return template_response(
        "browse_mam.html",
        request,
        user,
        {
            "search_term": q or "",
            "sections": sections,
            "request_id": request_id,
            "categories": audio_categories,
            "selected_category": category,
        },
    )
