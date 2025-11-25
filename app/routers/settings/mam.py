from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, Form, Request, Response, Security
from sqlmodel import Session

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.indexers.configuration import indexer_configuration_cache
from app.internal.models import GroupEnum
from app.internal.indexers.mam import MamConfigurations
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import template_response
from app.util.toast import ToastException

router = APIRouter(prefix="/mam")


@router.get("")
async def read_mam_settings(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    configs = MamConfigurations()
    values = {}
    for key in configs.model_fields.keys():
        values[key] = indexer_configuration_cache.get(session, f"MyAnonamouse_{key}") or ""
    return template_response(
        "settings_page/mam.html",
        request,
        admin_user,
        {
            "page": "mam",
            "values": values,
        },
    )


@router.post("")
async def update_mam_settings(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
    mam_session_id: Annotated[str, Form()] = "",
    download_client: Annotated[str, Form()] = "transmission",
    transmission_url: Annotated[str, Form()] = "",
    transmission_username: Annotated[str, Form()] = "",
    transmission_password: Annotated[str, Form()] = "",
    qbittorrent_url: Annotated[str, Form()] = "",
    qbittorrent_username: Annotated[str, Form()] = "",
    qbittorrent_password: Annotated[str, Form()] = "",
    seed_target_hours: Annotated[int, Form()] = 72,
    qbittorrent_seed_ratio: Annotated[str | None, Form()] = None,
    qbittorrent_seed_time: Annotated[str | None, Form()] = None,
    qbittorrent_remote_path_prefix: Annotated[str | None, Form()] = None,
    qbittorrent_local_path_prefix: Annotated[str | None, Form()] = None,
):
    if not mam_session_id.strip():
        raise ToastException("MAM session ID is required", "error")

    def _set(key: str, value: str):
        indexer_configuration_cache.set(session, f"MyAnonamouse_{key}", value)

    _set("mam_session_id", mam_session_id.strip())
    _set("download_client", download_client)
    _set("transmission_url", transmission_url)
    _set("transmission_username", transmission_username)
    _set("transmission_password", transmission_password)
    _set("qbittorrent_url", qbittorrent_url)
    _set("qbittorrent_username", qbittorrent_username)
    _set("qbittorrent_password", qbittorrent_password)
    _set("seed_target_hours", str(seed_target_hours))
    if qbittorrent_seed_ratio:
        _set("qbittorrent_seed_ratio", qbittorrent_seed_ratio)
    if qbittorrent_seed_time:
        _set("qbittorrent_seed_time", qbittorrent_seed_time)
    if qbittorrent_remote_path_prefix is not None:
        _set("qbittorrent_remote_path_prefix", qbittorrent_remote_path_prefix)
    if qbittorrent_local_path_prefix is not None:
        _set("qbittorrent_local_path_prefix", qbittorrent_local_path_prefix)

    raise ToastException("MAM settings saved", "success", cause_refresh=True)
