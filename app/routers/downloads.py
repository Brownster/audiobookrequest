import asyncio
import uuid
from pathlib import Path
from dataclasses import dataclass
from fastapi import APIRouter, Depends, Request, Security, HTTPException, Response
from fastapi import Form
from sqlalchemy import desc
from sqlmodel import Session, select
import os

from aiohttp import ClientSession

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import DownloadJob, DownloadJobStatus, GroupEnum, MediaType, BookRequest
from app.internal.processing.postprocess import AUDIO_EXTENSIONS
from app.internal.services.download_manager import DownloadManager
from app.internal.services.seeding import TorrentSeedConfiguration
from app.util.db import get_session
from app.util.templates import template_response
from app.internal.book_search import get_book_by_asin, get_region_from_settings
from app.util.connection import get_connection


router = APIRouter()


@dataclass
class BookCandidate:
    root: Path
    title: str
    authors: list[str]
    disc_folders: list[Path] | None = None  # For multi-disc books


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
    """Check if path contains audio files (synchronous helper)."""
    for ext in AUDIO_EXTENSIONS:
        try:
            # Use iterdir instead of rglob for better performance
            # Only check immediate children and one level deep
            if any(p.suffix.lower() == ext for p in path.glob("*") if p.is_file()):
                return True
            if any(p.suffix.lower() == ext for p in path.glob("*/*") if p.is_file()):
                return True
        except (PermissionError, OSError):
            continue
    # Fallback: deeper scan to avoid missing nested audio (e.g., Disc/Track folders)
    try:
        for ext in AUDIO_EXTENSIONS:
            if any(p.is_file() for p in path.rglob(f"*{ext}")):
                return True
    except (PermissionError, OSError):
        pass
    return False


def _has_ebook(path: Path) -> bool:
    """Check if path contains ebook files (synchronous helper)."""
    EBOOK_EXTENSIONS = {".epub", ".mobi", ".azw3", ".pdf", ".txt"}
    for ext in EBOOK_EXTENSIONS:
        try:
            # Check immediate children and one level deep
            if any(p.suffix.lower() == ext for p in path.glob("*") if p.is_file()):
                return True
            if any(p.suffix.lower() == ext for p in path.glob("*/*") if p.is_file()):
                return True
        except (PermissionError, OSError):
            continue
    return False


def _discover_books(base: Path, multi: bool, media_type: str = "audiobook") -> list[BookCandidate]:
    """
    If multi=True, treat each immediate subfolder containing audio/ebook as a separate book.
    Groups disc folders (e.g., "Book [Disc 1]", "Book [Disc 2]") into a single book.
    Otherwise treat the base as a single book.

    media_type: "audiobook" or "ebook" - determines which file types to look for
    """
    import re
    candidates: list[BookCandidate] = []

    # Choose detection function based on media type
    has_media = _has_ebook if media_type == "ebook" else _has_audio

    if multi:
        # Group folders by base title (strip disc markers)
        book_groups: dict[str, list[Path]] = {}
        disc_pattern = re.compile(r'\s*[\[\(]?\s*(disc|cd|disk)\s*\d+\s*[\]\)]?\s*$', re.IGNORECASE)

        for child in sorted(p for p in base.iterdir() if p.is_dir()):
            if not has_media(child):
                continue

            # Extract base book name by removing disc markers
            base_name = disc_pattern.sub('', child.name).strip()
            if base_name not in book_groups:
                book_groups[base_name] = []
            book_groups[base_name].append(child)

        # Create one candidate per book
        for base_name, disc_folders in sorted(book_groups.items()):
            # Use first disc folder as root, but track all disc folders
            first_folder = disc_folders[0]
            title, authors = _parse_title_author_from_path(base_name)
            parent_author, _ = _parse_title_author_from_path(base.name)
            if parent_author and not authors:
                authors = [parent_author]

            # If multiple disc folders, store them; otherwise just use single root
            candidates.append(BookCandidate(
                root=first_folder if len(disc_folders) == 1 else base,
                title=title or base_name,
                authors=authors,
                disc_folders=disc_folders if len(disc_folders) > 1 else None
            ))

        if candidates:
            return candidates

    # Fallback: single book at base
    title, authors = _parse_title_author_from_path(base.name)
    parent_author, _ = _parse_title_author_from_path(base.parent.name)
    if parent_author and not authors:
        authors = [parent_author]
    candidates.append(BookCandidate(root=base, title=title, authors=authors))
    return candidates


def _build_fake_snapshot_sync(source: Path, disc_folders: list[Path] | None = None) -> dict:
    """Build a torrent_snapshot-like dict for the post-processors."""
    if source.is_file():
        download_dir = source.parent
        files = [{"name": source.name}]
        name = source.name
    elif disc_folders:
        # Multi-disc book: include files from all disc folders
        download_dir = source
        # Use the base book name (strip disc markers from first folder)
        import re
        disc_pattern = re.compile(r'\s*[\[\(]?\s*(disc|cd|disk)\s*\d+\s*[\]\)]?\s*$', re.IGNORECASE)
        name = disc_pattern.sub('', disc_folders[0].name).strip()
        files = []
        # Sort disc folders by disc number
        sorted_discs = sorted(disc_folders, key=lambda p: p.name)
        for disc_folder in sorted_discs:
            for p in disc_folder.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(download_dir).as_posix()
                    files.append({"name": rel})
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


async def _build_fake_snapshot(source: Path, disc_folders: list[Path] | None = None) -> dict:
    """Build a torrent_snapshot-like dict for the post-processors (async to avoid blocking)."""
    return await asyncio.to_thread(_build_fake_snapshot_sync, source, disc_folders)


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
        books = await asyncio.to_thread(_discover_books, path, multi_books, media_type)
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

    books = await asyncio.to_thread(_discover_books, path, multi_books, media_type)
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
    errors: list[str] = []

    for candidate in books:
        # For multi-book imports, ALWAYS use the candidate's pre-parsed title/authors
        # to avoid all books getting the same title from form input
        if multi_books:
            final_title = candidate.title
            author_list = candidate.authors or ["Unknown Author"]
        else:
            # For single-book imports, allow form override
            final_title = title.strip() or candidate.title
            author_list = [a.strip() for a in authors.split(",") if a.strip()] or candidate.authors or ["Unknown Author"]

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

        snapshot = await _build_fake_snapshot(candidate.root, candidate.disc_folders)

        from app.util.log import logger
        logger.info(
            "Manual import: processing book",
            title=final_title,
            disc_count=len(candidate.disc_folders) if candidate.disc_folders else 0,
            file_count=len(snapshot.get("files", [])),
        )

        async with ClientSession() as http_session:
            try:
                if media_enum == MediaType.ebook:
                    processor = EbookPostProcessor(Path(settings.book_dir), tmp_dir, http_session)
                    dest = await asyncio.wait_for(processor.process(str(job_id), book, snapshot), timeout=300.0)
                else:
                    processor = PostProcessor(Path(settings.download_dir), tmp_dir, enable_merge=True, http_session=http_session)
                    dest = await asyncio.wait_for(processor.process(str(job_id), book, snapshot), timeout=300.0)
            except asyncio.TimeoutError:
                errors.append(f"{final_title}: Post-processing timed out after 5 minutes")
                continue
            except PostProcessingError as exc:
                errors.append(f"{final_title}: {exc}")
                continue
            except Exception as exc:
                errors.append(f"{final_title}: Unexpected error - {exc}")
                continue

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

    # Build success/error message
    success_msg = None
    error_msg = None
    if successes:
        success_msg = f"Successfully imported {len(successes)} of {len(books)} book(s)"
    if errors:
        error_msg = "; ".join(errors[:5])  # Show first 5 errors
        if len(errors) > 5:
            error_msg += f" ... and {len(errors) - 5} more errors"

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
            "success": success_msg,
            "error": error_msg,
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


@router.post("/downloads/manual/search-book")
async def manual_import_search_book(
    request: Request,
    session: Session = Depends(get_session),
    client_session: ClientSession = Depends(get_connection),
    query: str = Form(...),
    source_path: str = Form(...),
    media_type: str = Form("audiobook"),
    book_index: int = Form(0),
    user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
) -> Response:
    """
    Search Audible/Audnexus for a book based on query.
    Returns search results with auto-selected first result as suggestion.
    """
    from app.internal.book_search import list_audible_books
    from app.internal.audiobookshelf.client import abs_book_exists
    from app.internal.env_settings import Settings
    from app.util.log import logger

    region = get_region_from_settings()

    # Search Audible API
    logger.info("Manual import: search", query=query, source=source_path, index=book_index)
    results = await list_audible_books(
        session=session,
        client_session=client_session,
        query=query,
        num_results=20,
        page=0,
        audible_region=region,
    )

    # Check ABS for duplicates
    abs_config = Settings().audiobookshelf
    if abs_config and abs_config.host:
        for book in results:
            try:
                if await abs_book_exists(session, client_session, book):
                    book.downloaded = True
            except Exception:
                pass  # Ignore ABS check failures

    # Auto-select first result as suggestion
    suggested = results[0] if results else None

    return template_response(
        "components/manual_import_book_results.html",
        request,
        user,
        {
            "books": results,
            "suggested": suggested,
            "source_path": source_path,
            "media_type": media_type,
            "book_index": book_index,
            "query": query,
        },
    )


@router.post("/downloads/manual/select-book")
async def manual_import_select_book(
    request: Request,
    session: Session = Depends(get_session),
    client_session: ClientSession = Depends(get_connection),
    asin: str = Form(...),
    source_path: str = Form(...),
    media_type: str = Form("audiobook"),
    book_index: int = Form(0),
    user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
) -> Response:
    """
    User selected a book from search results.
    Fetch full metadata and show confirmation form.
    """
    from app.internal.audiobookshelf.client import abs_book_exists
    from app.internal.env_settings import Settings

    region = get_region_from_settings()
    book = await get_book_by_asin(client_session, asin, region)

    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    # Check if already in ABS
    duplicate_warning = None
    abs_config = Settings().audiobookshelf
    if abs_config and abs_config.host:
        try:
            if await abs_book_exists(session, client_session, book):
                duplicate_warning = "This book already exists in your Audiobookshelf library"
        except Exception:
            pass  # Ignore ABS check failures

    return template_response(
        "components/manual_import_confirm.html",
        request,
        user,
        {
            "book": book,
            "source_path": source_path,
            "media_type": media_type,
            "book_index": book_index,
            "duplicate_warning": duplicate_warning,
        },
    )


@router.post("/downloads/manual/import-with-metadata")
async def manual_import_with_metadata(
    request: Request,
    session: Session = Depends(get_session),
    client_session: ClientSession = Depends(get_connection),
    source_path: str = Form(...),
    asin: str = Form(...),
    media_type: str = Form("audiobook"),
    # Allow override of metadata fields
    title: str | None = Form(None),
    authors: str | None = Form(None),
    narrators: str | None = Form(None),
    series_name: str | None = Form(None),
    series_position: str | None = Form(None),
    user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
) -> Response:
    """
    Process the import with full metadata from Audible/Audnexus.
    """
    from app.internal.processing.postprocess import PostProcessor, EbookPostProcessor, PostProcessingError
    from app.internal.env_settings import Settings
    from app.util.log import logger
    from datetime import datetime

    region = get_region_from_settings()

    # Fetch book metadata from Audnexus
    book = await get_book_by_asin(client_session, asin, region)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found in Audible catalog")

    # Apply user overrides if provided
    if title and title.strip():
        book.title = title.strip()
    if authors and authors.strip():
        book.authors = [a.strip() for a in authors.split(",") if a.strip()]
    if narrators and narrators.strip():
        book.narrators = [n.strip() for n in narrators.split(",") if n.strip()]
    if series_name and series_name.strip():
        book.series_name = series_name.strip()
    if series_position and series_position.strip():
        book.series_position = series_position.strip()

    # Process through PostProcessor (similar to manual_import_run)
    path = Path(source_path).expanduser()
    if not await asyncio.to_thread(path.exists):
        raise HTTPException(status_code=404, detail=f"Source path does not exist: {path}")

    # Discover books (single book mode for now)
    books = await asyncio.to_thread(_discover_books, path, False, media_type)
    if not books:
        raise HTTPException(status_code=400, detail="No media files found at source path")

    candidate = books[0]
    settings = Settings().app
    tmp_dir = Path("/tmp/abr/manual-import")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    media_enum = MediaType(media_type)

    # Update book fields
    book.media_type = media_enum
    book.downloaded = True

    # Build snapshot for PostProcessor
    snapshot = await _build_fake_snapshot(candidate.root, candidate.disc_folders)

    job_id = uuid.uuid4()

    logger.info(
        "Manual import with metadata: processing book",
        title=book.title,
        asin=book.asin,
        authors=book.authors,
        narrators=book.narrators,
        series=book.series_name,
    )

    try:
        if media_enum == MediaType.ebook:
            processor = EbookPostProcessor(Path(settings.book_dir), tmp_dir, client_session)
            dest = await asyncio.wait_for(processor.process(str(job_id), book, snapshot), timeout=300.0)
        else:
            processor = PostProcessor(Path(settings.download_dir), tmp_dir, enable_merge=True, http_session=client_session)
            dest = await asyncio.wait_for(processor.process(str(job_id), book, snapshot), timeout=300.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Post-processing timed out after 5 minutes")
    except PostProcessingError as exc:
        raise HTTPException(status_code=500, detail=f"Post-processing failed: {exc}")
    except Exception as exc:
        logger.error("Manual import with metadata failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")

    # Create DownloadJob record
    job = DownloadJob(
        id=job_id,
        request_id=None,
        media_type=media_enum,
        status=DownloadJobStatus.completed,
        title=book.title,
        provider="manual",
        torrent_id=None,
        transmission_hash=None,
        transmission_id=None,
        seed_configuration={},
        destination_path=str(dest),
        message="Imported manually with metadata",
        created_at=datetime.utcnow(),
        completed_at=datetime.utcnow(),
    )
    session.add(job)
    session.commit()

    # Return success message
    return template_response(
        "components/manual_import_success.html",
        request,
        user,
        {
            "title": book.title,
            "destination": str(dest),
            "asin": book.asin,
        },
    )


@router.post("/downloads/manual/batch-search")
async def manual_import_batch_search(
    request: Request,
    session: Session = Depends(get_session),
    client_session: ClientSession = Depends(get_connection),
    source_path: str = Form(...),
    media_type: str = Form("audiobook"),
    user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
) -> Response:
    """
    For multi-book mode: discover all books and auto-search for each.
    Returns a table for user to review/confirm matches.
    """
    from app.internal.book_search import list_audible_books
    from app.internal.audiobookshelf.client import abs_book_exists
    from app.internal.env_settings import Settings
    from app.util.log import logger

    path = Path(source_path).expanduser()
    if not await asyncio.to_thread(path.exists):
        raise HTTPException(status_code=404, detail=f"Source path does not exist: {path}")

    # Discover all books in the folder
    books = await asyncio.to_thread(_discover_books, path, True, media_type)
    if not books:
        raise HTTPException(status_code=400, detail="No books found at source path")

    logger.info("Batch search: discovered books", count=len(books))

    region = get_region_from_settings()
    matches = []
    abs_config = Settings().audiobookshelf

    # For each book, auto-search and take first result as suggestion
    # Use asyncio.gather for parallel searching (faster for 50 books)
    async def search_book(candidate: BookCandidate, index: int):
        query = f"{candidate.title} {' '.join(candidate.authors)}"
        try:
            results = await list_audible_books(
                session=session,
                client_session=client_session,
                query=query,
                num_results=5,  # Get top 5 for each
                page=0,
                audible_region=region,
            )
            # Duplicate check per result (best-effort)
            if abs_config and abs_config.host:
                for book in results:
                    try:
                        if await abs_book_exists(session, client_session, book):
                            book.downloaded = True
                    except Exception:
                        pass
            return {
                "index": index,
                "candidate": candidate,
                "suggested_match": results[0] if results else None,
                "all_matches": results,
                "query": query,
            }
        except Exception as exc:
            logger.error("Batch search failed for book", title=candidate.title, error=str(exc))
            return {
                "index": index,
                "candidate": candidate,
                "suggested_match": None,
                "all_matches": [],
                "query": query,
                "error": str(exc),
            }

    # Search all books in parallel
    search_tasks = [search_book(book, idx) for idx, book in enumerate(books)]
    matches = await asyncio.gather(*search_tasks)

    logger.info("Batch search: completed", total=len(matches), found=sum(1 for m in matches if m["suggested_match"]))

    return template_response(
        "manual_import_batch_review.html",
        request,
        user,
        {
            "matches": matches,
            "source_path": source_path,
            "media_type": media_type,
            "base_url": request.url_for("manual_import_form").path.rstrip("/manual"),
        },
    )


@router.post("/downloads/manual/batch-import")
async def manual_import_batch_process(
    request: Request,
    session: Session = Depends(get_session),
    client_session: ClientSession = Depends(get_connection),
    source_path: str = Form(...),
    media_type: str = Form("audiobook"),
    user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
) -> Response:
    """
    Process multiple books with confirmed metadata in parallel.
    Accepts form data with asin_0, asin_1, etc. for confirmed books.
    """
    from app.internal.processing.postprocess import PostProcessor, EbookPostProcessor, PostProcessingError
    from app.internal.env_settings import Settings
    from app.util.log import logger
    from datetime import datetime

    # Parse confirmed books from form data
    form_data = await request.form()
    confirmed_books = []

    # Extract asin_{index} fields
    for key, value in form_data.items():
        if key.startswith("asin_") and value:
            try:
                index = int(key.split("_")[1])
                asin = value
                # Only include rows that were confirmed by the user
                if form_data.get(f"confirm_{index}"):
                    # Also get the source path for this specific book if provided
                    book_path = form_data.get(f"path_{index}", source_path)
                    confirmed_books.append({"index": index, "asin": asin, "path": book_path})
            except (ValueError, IndexError):
                continue

    base_url = request.url_for("manual_import_form").path.rstrip("/manual")

    if not confirmed_books:
        # Nothing was selected – return empty results panel
        return template_response(
            "components/manual_import_batch_results.html",
            request,
            user,
            {"results": [], "base_url": base_url},
        )

    logger.info("Batch import: starting", count=len(confirmed_books))

    region = get_region_from_settings()
    settings = Settings().app
    tmp_dir = Path("/tmp/abr/manual-import")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    media_enum = MediaType(media_type)

    successes = []
    errors = []

    # Process each confirmed book
    async def process_book(book_data: dict):
        asin = book_data["asin"]
        book_path = Path(book_data["path"]).expanduser()
        index = book_data["index"]

        try:
            # Fetch metadata
            book = await get_book_by_asin(client_session, asin, region)
            if not book:
                return {"success": False, "index": index, "error": f"Book {asin} not found in Audible catalog"}

            book.media_type = media_enum
            book.downloaded = True

            # Discover the specific book (single book mode for this path)
            books = await asyncio.to_thread(_discover_books, book_path, False, media_type)
            if not books:
                return {"success": False, "index": index, "error": f"No media files at {book_path}"}

            candidate = books[0]
            snapshot = await _build_fake_snapshot(candidate.root, candidate.disc_folders)
            job_id = uuid.uuid4()

            logger.info(
                "Batch import: processing book",
                title=book.title,
                index=index,
                asin=book.asin,
            )

            # Process through PostProcessor
            if media_enum == MediaType.ebook:
                processor = EbookPostProcessor(Path(settings.book_dir), tmp_dir, client_session)
                dest = await asyncio.wait_for(processor.process(str(job_id), book, snapshot), timeout=300.0)
            else:
                processor = PostProcessor(Path(settings.download_dir), tmp_dir, enable_merge=True, http_session=client_session)
                dest = await asyncio.wait_for(processor.process(str(job_id), book, snapshot), timeout=300.0)

            # Create DownloadJob record
            job = DownloadJob(
                id=job_id,
                request_id=None,
                media_type=media_enum,
                status=DownloadJobStatus.completed,
                title=book.title,
                provider="manual",
                torrent_id=None,
                transmission_hash=None,
                transmission_id=None,
                seed_configuration={},
                destination_path=str(dest),
                message="Imported manually with metadata (batch)",
                created_at=datetime.utcnow(),
                completed_at=datetime.utcnow(),
            )
            session.add(job)
            session.commit()

            return {
                "success": True,
                "index": index,
                "title": book.title,
                "authors": ", ".join(book.authors or []),
                "asin": book.asin,
                "destination": str(dest),
                "job_id": str(job.id),
            }

        except asyncio.TimeoutError:
            return {
                "success": False,
                "index": index,
                "asin": asin,
                "error": "Processing timed out after 5 minutes",
            }
        except PostProcessingError as exc:
            return {
                "success": False,
                "index": index,
                "asin": asin,
                "error": f"Post-processing failed: {exc}",
            }
        except Exception as exc:
            logger.error("Batch import: book failed", index=index, error=str(exc))
            return {
                "success": False,
                "index": index,
                "asin": asin,
                "error": str(exc),
            }

    # Process all books in parallel (but limit concurrency to avoid overwhelming system)
    # Process in batches of 5 at a time
    batch_size = 5
    all_results = []

    for i in range(0, len(confirmed_books), batch_size):
        batch = confirmed_books[i:i + batch_size]
        logger.info(f"Processing batch {i // batch_size + 1}/{(len(confirmed_books) + batch_size - 1) // batch_size}")
        batch_results = await asyncio.gather(*[process_book(book) for book in batch])
        all_results.extend(batch_results)

    # Separate successes and errors
    for result in all_results:
        if result["success"]:
            successes.append(result)
        else:
            errors.append(result)

    logger.info("Batch import: completed", successes=len(successes), errors=len(errors))

    # Combine and sort results to feed the component
    combined_results = successes + errors
    combined_results.sort(key=lambda r: r.get("index", 0))

    # Return results
    return template_response(
        "components/manual_import_batch_results.html",
        request,
        user,
        {
            "results": combined_results,
            "base_url": base_url,
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
