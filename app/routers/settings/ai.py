from typing import Annotated

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, Form, Request, Response, Security
from sqlmodel import Session

from app.internal.auth.authentication import ABRAuth, DetailedUser
from app.internal.models import GroupEnum
from app.internal.ai.config import ai_config
from app.util.connection import get_connection
from app.util.db import get_session
from app.util.templates import template_response


router = APIRouter(prefix="/ai")


@router.get("")
async def read_ai_settings(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    endpoint = ai_config.get_endpoint(session) or ""
    model = ai_config.get_model(session) or ""
    provider = ai_config.get_provider(session) or "ollama"
    api_key = ai_config.get_api_key(session) or ""
    return template_response(
        "settings_page/ai.html",
        request,
        admin_user,
        {
            "page": "ai",
            "ai_endpoint": endpoint,
            "ai_model": model,
            "ai_provider": provider,
            "ai_api_key": api_key,
        },
    )


@router.post("/config")
def update_ai_config(
    session: Annotated[Session, Depends(get_session)],
    provider: Annotated[str, Form(alias="provider")],
    endpoint: Annotated[str, Form(alias="endpoint")],
    model: Annotated[str, Form(alias="model")],
    api_key: Annotated[str | None, Form(alias="api_key")] = None,
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    ai_config.set_provider(session, provider.strip() or "ollama")
    ai_config.set_endpoint(session, endpoint.strip())
    ai_config.set_model(session, model)
    if api_key is not None:
        ai_config.set_api_key(session, api_key.strip())
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/test")
async def test_ai_connection(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
    admin_user: DetailedUser = Security(ABRAuth(GroupEnum.admin)),
):
    """Attempt to contact the configured provider and verify availability. Returns a tiny HTML snippet suitable for HTMX target."""
    from fastapi import Response as FastAPIResponse

    provider = ai_config.get_provider(session)
    endpoint = ai_config.get_endpoint(session)
    model = ai_config.get_model(session)
    api_key = ai_config.get_api_key(session)
    status: str
    detail: str = ""
    ok = False
    if not endpoint or not model or (provider == "openai" and not api_key):
        status = "not_configured"
        detail = "Endpoint or model not set (and API key for OpenAI)."
    else:
        if provider == "openai":
            try:
                url = f"{endpoint}/v1/chat/completions"
                body = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": "Return JSON: {\"ping\":\"pong\"}"},
                    ],
                    "response_format": {"type": "json_object"},
                }
                headers = {"Authorization": f"Bearer {api_key}"}
                async with client_session.post(url, json=body, headers=headers, timeout=10) as resp:
                    data = await resp.json(content_type=None)
                    content = ""
                    if isinstance(data, dict):
                        try:
                            content = data["choices"][0]["message"]["content"]
                        except Exception:
                            content = ""
                    if resp.status == 200 and "pong" in content:
                        ok = True
                        status = "ok"
                        detail = "OpenAI chat completion succeeded."
                    else:
                        status = "generate_failed"
                        detail = f"OpenAI returned {resp.status}: {data}"
            except Exception as e:
                ok = False
                status = "generate_failed"
                detail = f"OpenAI call failed: {e}"
        else:
            # Ollama check
            try:
                async with client_session.get(f"{endpoint}/api/tags", timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        tags = [t.get("name") for t in data.get("models", [])] if isinstance(data, dict) else []
                        if model in (tags or []):
                            ok = True
                            status = "ok"
                            detail = f"Model '{model}' is available."
                        else:
                            ok = False
                            status = "model_missing"
                            detail = f"Model '{model}' not found among available models."
                    else:
                        ok = False
                        status = "unreachable"
                        detail = f"Tags endpoint returned status {resp.status}."
            except Exception as e:
                ok = False
                status = "unreachable"
                detail = f"Failed to reach endpoint: {e}"

    # Decide alert style based on status
    if ok:
        cls = "alert-success"
    elif status in {"model_missing", "unreachable", "generate_failed", "not_configured"}:
        cls = "alert-error"
    else:
        cls = "alert-warning"

    html = f"<div class='alert {cls}'><span>{detail or status}</span></div>"
    return FastAPIResponse(content=html, media_type="text/html")
