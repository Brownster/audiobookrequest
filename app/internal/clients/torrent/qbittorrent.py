from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from enum import Enum
from http.cookies import SimpleCookie
from typing import Any, Dict, Iterable, Optional

from aiohttp import ClientError, ClientSession, FormData
from aiohttp.client_exceptions import (
    ClientConnectorCertificateError,
    ClientConnectorSSLError,
)
from yarl import URL

from app.util.log import logger
from app.internal.clients.torrent.abstract import AbstractTorrentClient


class QbitContentLayout(str, Enum):
    """Content layout handling options available in qBittorrent."""

    default = "default"
    original = "original"
    subfolder = "subfolder"


class QbitClientError(RuntimeError):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class AuthenticationError(QbitClientError):
    def __init__(self):
        super().__init__(
            "Authentication failed",
            hint="Check the username and password in settings.",
        )


class CertValidationError(QbitClientError):
    def __init__(self):
        super().__init__(
            "SSL certificate validation failed",
            hint="The qBittorrent server is using a self-signed certificate. "
            "Configure the system to trust it or disable SSL verification (not recommended).",
        )


class HTTPStatusError(QbitClientError):
    def __init__(self, status: int, reason: str, body: str, hint: str | None = None):
        self.status = status
        self.reason = reason
        self.body = body
        super().__init__(f"HTTP {status} {reason}", hint=hint)


class QueueingDisabledError(QbitClientError):
    def __init__(self):
        super().__init__(
            "Torrent queueing is disabled",
            hint="The qBittorrent server has reached its active torrent limit. "
            "Increase the queue size limits in qBittorrent settings.",
        )


class CapabilityProbeError(QbitClientError):
    pass


@dataclass(frozen=True)
class QbitCapabilities:
    """Represents the supported qBittorrent Web API surface."""

    api_major: int
    supported_endpoints: frozenset[str]

    _PROBE_ENDPOINTS: tuple[str, ...] = (
        "/api/v2/app/webapiVersion",
        "/api/v2/app/version",
        "/version/api",
    )

    @classmethod
    async def probe(
        cls,
        session: ClientSession,
        base_url: str,
        *,
        timeout: float | None = None,
        auth: Any | None = None,
    ) -> "QbitCapabilities":
        """Query the qBittorrent Web API to learn which endpoints are supported."""

        base = base_url.rstrip("/")
        found: set[str] = set()
        api_major: int | None = None
        for path in cls._PROBE_ENDPOINTS:
            url = cls._join_url(base, path)
            try:
                async with session.get(url, timeout=timeout, auth=auth) as response:
                    if response.status >= 400:
                        logger.debug(
                            "qBittorrent: capability probe failed", path=path, status=response.status
                        )
                        continue
                    body = await response.text()
                    found.add(path)
                    if api_major is None:
                        api_major = cls._parse_major(body)
                        if api_major is not None:
                            logger.info(
                                "qBittorrent: detected Web API", api_major=api_major, path=path
                            )
            except asyncio.TimeoutError:
                logger.warning("qBittorrent: capability probe timeout", path=path)
            except Exception as exc:
                logger.debug("qBittorrent: capability probe exception", path=path, error=str(exc))
        if api_major is None:
            raise CapabilityProbeError(
                "Unable to determine qBittorrent Web API version."
            )
        if not found:
            raise CapabilityProbeError(
                "qBittorrent Web API does not expose a recognizable version endpoint."
            )
        return cls(api_major=api_major, supported_endpoints=frozenset(found))

    @staticmethod
    def _join_url(base: str, path: str) -> str:
        return f"{base.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _parse_major(raw: str) -> int | None:
        match = re.search(r"(\d+)", raw)
        if not match:
            return None
        return int(match.group(1))

    def supports(self, endpoint: str) -> bool:
        normalized = endpoint if endpoint.startswith("/") else f"/{endpoint.lstrip('/')}"
        return normalized in self.supported_endpoints

    def prefers_v2(self) -> bool:
        return any(path.startswith("/api/v2") for path in self.supported_endpoints)


@dataclass(frozen=True)
class QbitAddOptions:
    """Configurable qBittorrent parameters for new torrents."""

    category: str | None = None
    start_paused: bool | None = None
    force_start: bool | None = None
    sequential: bool | None = None
    content_layout: QbitContentLayout | None = None
    ratio_limit: float | None = None
    seeding_time_limit: int | None = None
    tags: list[str] | None = None


@dataclass(frozen=True)
class QbitAddRequest:
    """Represents the endpoint + payload used to add a torrent."""

    path: str
    form_fields: dict[str, str] = field(default_factory=dict)


class QbitAddRequestBuilder:
    """Translates ``QbitAddOptions`` into API-specific form fields."""

    def __init__(self, capabilities: QbitCapabilities) -> None:
        self._capabilities = capabilities

    def build(self, options: QbitAddOptions | None = None) -> QbitAddRequest:
        use_v2 = self._capabilities.prefers_v2()
        path = "api/v2/torrents/add" if use_v2 else "command/upload"
        opts = options or QbitAddOptions()
        fields: dict[str, str] = {}
        if opts.category:
            fields["category"] = opts.category
        if opts.start_paused is not None:
            pause_key = "stopped" if use_v2 else "paused"
            fields[pause_key] = self._bool_str(opts.start_paused)
        if opts.force_start is not None:
            force_key = "forced" if use_v2 else "forceStart"
            fields[force_key] = self._bool_str(opts.force_start)
        if opts.sequential:
            fields["sequentialDownload"] = self._bool_str(True)
        if opts.content_layout and opts.content_layout != QbitContentLayout.default:
            layout_value = self._map_content_layout(opts.content_layout)
            if layout_value:
                fields["contentLayout"] = layout_value
        if opts.ratio_limit is not None:
            fields["ratioLimit"] = str(opts.ratio_limit)
        if opts.seeding_time_limit is not None:
            fields["seedingTimeLimit"] = str(opts.seeding_time_limit)
        if opts.tags:
            fields["tags"] = ",".join(opts.tags)
        return QbitAddRequest(path=path, form_fields=fields)

    @staticmethod
    def _bool_str(value: bool) -> str:
        return "true" if value else "false"

    @staticmethod
    def _map_content_layout(layout: QbitContentLayout) -> str | None:
        if layout == QbitContentLayout.original:
            return "Original"
        if layout == QbitContentLayout.subfolder:
            return "Subfolder"
        return None


class QbitClient(AbstractTorrentClient):
    """Thin wrapper around the qBittorrent WebUI API (v2)."""

    _COOKIE_CACHE: dict[str, SimpleCookie[str]] = {}

    def __init__(
        self,
        http_session: ClientSession,
        base_url: str,
        username: str,
        password: str,
        capabilities: QbitCapabilities | None = None,
    ):
        self._session = http_session
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._basic_auth = None
        if username and password:
            try:
                from aiohttp import BasicAuth
                self._basic_auth = BasicAuth(username, password)
            except Exception:
                self._basic_auth = None
        self._capabilities = capabilities
        self._authenticated = False
        self._cookie_key = self._base_url
        self._load_cached_cookies()

    def _build_url(self, path: str) -> str:
        return f"{self._base_url.rstrip('/')}/{path.lstrip('/')}"

    async def _ensure_capabilities(self):
        if self._capabilities is None:
            try:
                self._capabilities = await QbitCapabilities.probe(
                    self._session, self._base_url, auth=self._basic_auth
                )
            except CapabilityProbeError as exc:
                # Fallback to v2 defaults if probe fails (common when auth is required before probe)
                logger.warning("qBittorrent: capability probe failed, using defaults", error=str(exc))
                self._capabilities = QbitCapabilities(
                    api_major=2,
                    supported_endpoints=frozenset(
                        ["/api/v2/app/webapiVersion", "/api/v2/app/version", "/version/api"]
                    ),
                )

    @property
    def capabilities(self) -> QbitCapabilities | None:
        return self._capabilities

    def _load_cached_cookies(self) -> None:
        cached = self._COOKIE_CACHE.get(self._cookie_key)
        if cached:
            self._session.cookie_jar.update_cookies(cached, URL(self._base_url))
            self._authenticated = True

    def _persist_cookies(self) -> None:
        cookies = self._session.cookie_jar.filter_cookies(URL(self._base_url))
        if cookies:
            self._COOKIE_CACHE[self._cookie_key] = cookies

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        url = self._build_url(path)
        for attempt in range(2):
            try:
                if "auth" not in kwargs and self._basic_auth:
                    kwargs["auth"] = self._basic_auth
                async with self._session.request(method, url, **kwargs) as resp:
                    if resp.status == 403 and attempt == 0:
                        await self._handle_forbidden()
                        continue
                    return await self._decode_response(resp)
            except (ClientConnectorCertificateError, ClientConnectorSSLError) as exc:
                raise CertValidationError() from exc
            except ClientError as exc:
                raise QbitClientError(
                    f"qBittorrent request failed: {exc}",
                    hint="Ensure the qBittorrent URL is reachable from the server running Mamlarr.",
                ) from exc
        raise AuthenticationError()

    async def _handle_forbidden(self) -> None:
        self._authenticated = False
        await self._login(force=True)

    async def _decode_response(self, resp):
        if resp.status >= 400:
            await self._handle_http_error(resp)
        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return await resp.json()
        return await resp.text()

    async def _handle_http_error(self, resp) -> None:
        body = await resp.text()
        lower_body = body.lower()
        logger.warning(
            "qBittorrent HTTP error",
            status=resp.status,
            reason=resp.reason,
            body_preview=body[:200].strip(),
            url=str(resp.url),
        )
        if resp.status in (401, 403):
            raise AuthenticationError()
        if resp.status == 409 and "queue" in lower_body:
            raise QueueingDisabledError()
        hint: str | None = None
        if resp.status == 404:
            hint = "Confirm the qBittorrent version exposes the WebUI API at this base path."
        elif resp.status == 415:
            hint = "qBittorrent could not parse the upload. Update the client or retry with a known-good .torrent file."
        elif resp.status == 429:
            hint = "qBittorrent is rate limiting API calls. Reduce the polling frequency or wait a moment."
        elif resp.status >= 500:
            hint = "The qBittorrent WebUI reported a server error. Check its logs for more details."
        raise HTTPStatusError(resp.status, resp.reason, body, hint=hint)

    async def _login(self, *, force: bool = False) -> None:
        if self._authenticated and not force:
            return
        data = {
            "username": self._username,
            "password": self._password,
        }
        async with self._session.post(
            self._build_url("api/v2/auth/login"),
            data=data,
            auth=self._basic_auth,
        ) as resp:
            body = await resp.text()
            if resp.status != 200 or body.strip() != "Ok.":
                raise AuthenticationError()
        self._authenticated = True
        self._persist_cookies()
        logger.info(
            "qBittorrent: authenticated", api_major=self._capabilities.api_major if self._capabilities else "?"
        )

    async def _ensure_auth(self) -> None:
        await self._login()

    async def add_torrent(
        self, torrent_bytes: bytes, **kwargs
    ) -> dict:
        await self._ensure_auth()
        await self._ensure_capabilities()
        assert self._capabilities is not None
        
        # Extract options from kwargs
        options = QbitAddOptions(
            category=kwargs.get("category"),
            start_paused=kwargs.get("start_paused"),
            force_start=kwargs.get("force_start"),
            sequential=kwargs.get("sequential"),
            content_layout=kwargs.get("content_layout"),
            ratio_limit=kwargs.get("ratio_limit"),
            seeding_time_limit=kwargs.get("seeding_time_limit"),
            tags=kwargs.get("tags"),
        )
        expected_name = kwargs.get("expected_name")
        expected_tag = kwargs.get("expected_tag")
        request = QbitAddRequestBuilder(self._capabilities).build(options)
        form = FormData()
        form.add_field(
            "torrents",
            torrent_bytes,
            filename="download.torrent",
            content_type="application/x-bittorrent",
        )
        for key, value in request.form_fields.items():
            form.add_field(key, value)
        if expected_tag:
            form.add_field("tags", expected_tag)
        await self._request("POST", request.path, data=form)
        # qBittorrent does not return the hash; fetch recently added and try to match name
        torrents = await self._request(
            "GET",
            "api/v2/torrents/info",
            params={"sort": "added_on", "reverse": "true"},
        )
        result: dict[str, Any] = {}
        if isinstance(torrents, list) and torrents:
            filtered = []
            if expected_tag:
                filtered = [t for t in torrents if expected_tag in str(t.get("tags", ""))]
            match = None
            pool = filtered or torrents
            if expected_name:
                en = expected_name.lower()
                for t in pool[:10]:
                    name = str(t.get("name") or "").lower()
                    if en in name:
                        match = t
                        break
            latest = match or (pool[0] if pool else None)
            if latest:
                result = {
                    "hashString": latest.get("hash"),
                    "id": latest.get("id") or latest.get("hash"),
                    "name": latest.get("name"),
                }
        logger.info("qBittorrent: torrent added", hash=result.get("hashString"))
        return result

    async def get_torrents(self, hashes: Iterable[str]) -> dict[str, dict[str, Any]]:
        await self._ensure_auth()
        params: Dict[str, Any] = {}
        hash_list = list(hashes)
        if hash_list:
            params["hashes"] = "|".join(hash_list)
        data = await self._request("GET", "api/v2/torrents/info", params=params)
        if isinstance(data, list):
            return {t.get("hash", ""): t for t in data if t.get("hash")}
        return {}

    async def remove_torrent(self, hash_string: str, delete_data: bool = False) -> None:
        await self._ensure_auth()
        await self._request(
            "POST",
            "api/v2/torrents/delete",
            data={
                "hashes": hash_string,
                "deleteFiles": "true" if delete_data else "false",
            },
        )
        logger.info("qBittorrent: torrent removed", hash=hash_string)

    async def test_connection(self) -> None:
        await self._ensure_capabilities()
        await self._ensure_auth()
        await self._request("GET", "api/v2/app/version")

    async def list_files(self, hash_string: str) -> list[dict]:
        await self._ensure_auth()
        data = await self._request(
            "GET", "api/v2/torrents/files", params={"hash": hash_string}
        )
        if isinstance(data, list):
            return data
        return []

    async def resume(self, hash_string: str) -> None:
        await self._ensure_auth()
        await self._request(
            "POST",
            "api/v2/torrents/resume",
            data={"hashes": hash_string},
        )

    async def set_share_limits(
        self,
        hash_string: str,
        *,
        ratio_limit: float | None = None,
        seeding_time_limit: int | None = None,
    ) -> None:
        await self._ensure_auth()
        payload: Dict[str, str] = {"hashes": hash_string}
        if ratio_limit is not None:
            payload["ratioLimit"] = str(ratio_limit)
        if seeding_time_limit is not None:
            payload["seedingTimeLimit"] = str(seeding_time_limit)
        if len(payload) == 1:
            return
        try:
            await self._request("POST", "api/v2/torrents/setShareLimits", data=payload)
        except HTTPStatusError as exc:
            logger.warning(
                "qBittorrent: setShareLimits failed, continuing",
                status=exc.status,
                reason=getattr(exc, "reason", ""),
                body=getattr(exc, "body", "")[:200] if hasattr(exc, "body") else "",
            )
