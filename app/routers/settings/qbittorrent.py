from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Security
from sqlmodel import Session

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.indexers.configuration import indexer_configuration_cache
from app.internal.models import GroupEnum
from app.util.db import get_session
from app.util.templates import template_response
from app.util.toast import ToastException

router = APIRouter(prefix="/qbittorrent")


def _get(key: str, session: Session) -> str:
    return indexer_configuration_cache.get(session, f"MyAnonamouse_{key}") or ""


@router.get("")
async def read_qbittorrent_settings(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    from app.internal.env_settings import Settings

    settings = Settings()

    values = {
        "qbittorrent_url": _get("qbittorrent_url", session),
        "qbittorrent_username": _get("qbittorrent_username", session),
        "qbittorrent_password": _get("qbittorrent_password", session),
        "seed_target_hours": _get("seed_target_hours", session) or "72",
        "qbittorrent_seed_ratio": _get("qbittorrent_seed_ratio", session),
        "qbittorrent_seed_time": _get("qbittorrent_seed_time", session),
        "qbittorrent_remote_path_prefix": _get("qbittorrent_remote_path_prefix", session),
        "qbittorrent_local_path_prefix": _get("qbittorrent_local_path_prefix", session),
        # Library paths (from environment variables)
        "download_dir": settings.app.download_dir,
        "book_dir": settings.app.book_dir,
    }
    return template_response(
        "settings_page/qbittorrent.html",
        request,
        admin_user,
        {
            "page": "qbittorrent",
            "values": values,
        },
    )


@router.post("")
async def update_qbittorrent_settings(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
    qbittorrent_url: Annotated[str, Form()] = "",
    qbittorrent_username: Annotated[str, Form()] = "",
    qbittorrent_password: Annotated[str, Form()] = "",
    seed_target_hours: Annotated[int, Form()] = 72,
    qbittorrent_seed_ratio: Annotated[str | None, Form()] = None,
    qbittorrent_seed_time: Annotated[str | None, Form()] = None,
    qbittorrent_remote_path_prefix: Annotated[str | None, Form()] = None,
    qbittorrent_local_path_prefix: Annotated[str | None, Form()] = None,
):
    def _set(key: str, value: str):
        indexer_configuration_cache.set(session, f"MyAnonamouse_{key}", value)

    # Force qBittorrent as the client choice for MAM downloads
    _set("download_client", "qbittorrent")

    _set("qbittorrent_url", qbittorrent_url.strip())
    _set("qbittorrent_username", qbittorrent_username.strip())
    _set("qbittorrent_password", qbittorrent_password)
    _set("seed_target_hours", str(seed_target_hours))
    _set("qbittorrent_seed_ratio", qbittorrent_seed_ratio or "")
    _set("qbittorrent_seed_time", qbittorrent_seed_time or "")
    if qbittorrent_remote_path_prefix is not None:
        _set("qbittorrent_remote_path_prefix", qbittorrent_remote_path_prefix)
    if qbittorrent_local_path_prefix is not None:
        _set("qbittorrent_local_path_prefix", qbittorrent_local_path_prefix)

    raise ToastException("qBittorrent settings saved", "success", cause_refresh=True)
