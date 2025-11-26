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
):
    if not mam_session_id.strip():
        raise ToastException("MAM session ID is required", "error")

    def _set(key: str, value: str):
        indexer_configuration_cache.set(session, f"MyAnonamouse_{key}", value)

    _set("mam_session_id", mam_session_id.strip())

    raise ToastException("MAM settings saved", "success", cause_refresh=True)
