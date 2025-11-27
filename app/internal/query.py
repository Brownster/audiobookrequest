# what is currently being queried
from contextlib import contextmanager
from typing import Any, Literal, Optional

import pydantic
from aiohttp import ClientSession
from fastapi import HTTPException
from sqlmodel import Session, select

from app.internal.models import BookRequest, User
from app.util.log import logger

querying: set[str] = set()


@contextmanager
def manage_queried(asin: str):
    querying.add(asin)
    try:
        yield
    finally:
        try:
            querying.remove(asin)
        except KeyError:
            pass


class QueryResult(pydantic.BaseModel):
    sources: Optional[list[Any]]
    book: BookRequest
    state: Literal["ok", "querying", "uncached"]

    @property
    def ok(self) -> bool:
        return self.state == "ok"


async def query_sources(
    asin: str,
    session: Session,
    client_session: ClientSession,
    requester: User,
    force_refresh: bool = False,
    start_auto_download: bool = False,
    only_return_if_cached: bool = False,
) -> QueryResult:
    book = session.exec(select(BookRequest).where(BookRequest.asin == asin)).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    if asin in querying:
        return QueryResult(
            sources=None,
            book=book,
            state="querying",
        )

    with manage_queried(asin):
        if start_auto_download:
            logger.info(
                "Prowlarr integration disabled; skipping auto-download for %s", asin
            )

        return QueryResult(sources=[], book=book, state="ok")
