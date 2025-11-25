from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, Sequence
from uuid import uuid4
from urllib.parse import urlencode, urljoin

from aiohttp import ClientSession
from pydantic import BaseModel, Field

from app.util.log import logger
from app.internal.clients.mam_categories import tracker_categories_for_torznab


class SearchType(str, Enum):
    """Subset of search type choices exposed by Jackett for MAM."""

    active = "active"
    dying = "dying"
    dead = "dead"
    all = "all"


class MamClientSettings(BaseModel):
    """Configuration for the MyAnonamouse client."""

    mam_session_id: str
    mam_base_url: str = "https://www.myanonamouse.net"
    search_type: SearchType = SearchType.all
    search_in_description: bool = False
    search_in_series: bool = True
    search_in_filenames: bool = False
    search_languages: list[int] = Field(default_factory=list)
    search_category_id: int = 13
    torrent_download_endpoint: str = "/torrents.php?action=download&id={id}"
    use_mock_data: bool = False


class MyAnonamouseClientError(RuntimeError):
    pass


class AuthenticationError(MyAnonamouseClientError):
    pass


class SearchError(MyAnonamouseClientError):
    pass


MOCK_RESULTS = [
    {
        "id": 1001,
        "title": "Mock Audiobook 1 - The Beginning",
        "seeders": 15,
        "leechers": 0,
        "size": 123_456_789,
        "tor_id": 1001,
        "language": "EN",
        "filetype": "M4B",
        "added": "2024-01-01T00:00:00Z",
        "author_info": json.dumps(["Mock Author A"]),
        "cat_name": "Audiobooks",
    },
    {
        "id": 1002,
        "title": "Mock Audiobook 2 - The Sequel",
        "seeders": 5,
        "leechers": 2,
        "size": 234_567_890,
        "tor_id": 1002,
        "language": "EN",
        "filetype": "MP3",
        "added": "2024-01-02T00:00:00Z",
        "author_info": json.dumps(["Mock Author A"]),
        "cat_name": "Audiobooks",
        "vip": 1,
    },
    {
        "id": 1003,
        "title": "Mock Audiobook 3 - A New Hope",
        "seeders": 100,
        "leechers": 10,
        "size": 345_678_901,
        "tor_id": 1003,
        "language": "EN",
        "filetype": "M4B",
        "added": "2024-01-03T00:00:00Z",
        "author_info": json.dumps(["Mock Author B"]),
        "cat_name": "Audiobooks",
        "free": 1,
    }
]


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_datetime(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (TypeError, ValueError):
            pass
    return datetime.now(tz=timezone.utc).isoformat()

def _coerce_dl_hash(raw: dict[str, Any]) -> str | None:
    """Extract a direct download hash if present."""
    for key in ("dl", "dl_hash", "torrent_hash", "hash"):
        val = raw.get(key)
        if val is not None:
            s = str(val).strip()
            if s:
                return s
    return None


def _coerce_title(result: dict[str, Any], fallback: str) -> str:
    for key in (
        "title",
        "name",
        "torTitle",
        "torname",
        "rawName",
        "book_title",
        "torrent_name",
    ):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _extract_size(raw: dict[str, Any]) -> int:
    for key in ("size", "size_bytes", "bytes", "filesize", "torrent_size"):
        if raw.get(key) is not None:
            return _coerce_int(raw[key])
    return 0


def _extract_seeders(raw: dict[str, Any]) -> int:
    for key in ("seeders", "seed", "seeders_total", "leech_seeders"):
        if raw.get(key) is not None:
            return _coerce_int(raw[key])
    return 0


def _extract_leechers(raw: dict[str, Any]) -> int:
    for key in ("leechers", "leeches", "leech", "leechers_total"):
        if raw.get(key) is not None:
            return _coerce_int(raw[key])
    return 0


def _determine_guid(raw: dict[str, Any]) -> str:
    for key in ("id", "tid", "tor_id", "torrent_id"):
        value = raw.get(key)
        if value:
            return f"mam-{value}"
    return f"mam-{uuid4()}"


def _flags_from_result(raw: dict[str, Any]) -> list[str]:
    flags: set[str] = set()
    if raw.get("personal_freeleech") in (1, "1", True):
        flags.update({"personal_freeleech", "freeleech"})
    if raw.get("free") in (1, "1", True):
        flags.update({"free", "freeleech"})
    if raw.get("fl_vip") in (1, "1", True):
        flags.update({"fl_vip", "freeleech"})
    if raw.get("vip") in (1, "1", True):
        flags.add("vip")
    return sorted(flags)


def _parse_people(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if isinstance(v, (str, int)) and str(v).strip()]
    if isinstance(value, dict):
        return [str(v).strip() for v in value.values() if v and str(v).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return _parse_people(parsed)
        except Exception:
            value = value.strip()
            return [value] if value else []
    return []


def _first_value(raw: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@dataclass
class MamSearchResult:
    guid: str
    title: str
    link: str
    details: str
    size: int
    seeders: int
    leechers: int
    peers: int
    publish_date: str
    download_volume_factor: float
    minimum_seed_time: int
    flags: list[str]
    raw: dict[str, Any] = field(repr=False)


class MyAnonamouseClient:
    """Thin wrapper around the MyAnonamouse JSON search endpoint."""

    MINIMUM_SEED_TIME = 259200  # 72 hours

    def __init__(self, http_session: ClientSession, settings: MamClientSettings):
        self._http_session = http_session
        self._settings = settings

    _QUERY_SANITIZER = re.compile(r"[^\w]+", re.IGNORECASE)

    def _sanitize_query(self, query: str) -> str:
        """Normalize the search term to match Jackett's behaviour."""

        sanitized = self._QUERY_SANITIZER.sub(" ", query or "").strip()
        return sanitized

    async def search(
        self,
        query: str,
        limit: int = 100,
        offset: int = 0,
        *,
        categories: Sequence[int] | Iterable[int] | None = None,
        languages: Sequence[int] | Iterable[int] | None = None,
    ) -> list[MamSearchResult]:
        sanitized_query = self._sanitize_query(query)
        if self._settings.use_mock_data:
            return self._normalize_results(MOCK_RESULTS, sanitized_query or query)
        sanitized_query = self._sanitize_query(query)
        if query and query.strip() and not sanitized_query:
            logger.debug(
                "MamService: search term empty after sanitization", query=query
            )
            return []

        # MAM responds reliably when using the JSON POST variant (mirrors Jackett/audiofinder behaviour).
        srch_in: list[str] = ["title", "author", "narrator"]
        if self._settings.search_in_description:
            srch_in.append("description")
        if self._settings.search_in_series:
            srch_in.append("series")
        if self._settings.search_in_filenames:
            srch_in.append("filename")

        selected_languages: Iterable[int] | Sequence[int] | None = (
            languages if languages else self._settings.search_languages
        )
        browse_lang = []
        if selected_languages:
            for lang in selected_languages:
                try:
                    browse_lang.append(str(int(lang)))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue

        tracker_categories = tracker_categories_for_torznab(categories)
        if not tracker_categories and self._settings.search_category_id:
            tracker_categories = [self._settings.search_category_id]
        main_cat = [str(cat) for cat in tracker_categories] if tracker_categories else ["13"]

        body: Dict[str, Any] = {
            "tor": {
                "text": sanitized_query,
                "searchType": self._settings.search_type.value,
                "srchIn": srch_in,
                "searchIn": "torrents",
                "sortType": "default",
                "startNumber": str(max(0, offset)),
                "main_cat": main_cat,
                "thumbnails": "1",
                "description": "1",
            },
            "perpage": str(max(1, limit) if limit else 100),
            "dlLink": "1",
        }
        if browse_lang:
            body["tor"]["browse_lang"] = browse_lang

        endpoint = "/tor/js/loadSearchJSONbasic.php"
        url = urljoin(self._settings.mam_base_url, endpoint)

        logger.debug("MamService: querying MAM", url=url)
        cookie_kwargs = self._cookie_kwargs()
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, */*",
            "Content-Type": "application/json",
            "Origin": self._settings.mam_base_url.rstrip("/"),
            "Referer": f"{self._settings.mam_base_url.rstrip('/')}/",
        }
        if "headers" in cookie_kwargs:
            headers.update(cookie_kwargs.pop("headers"))
        request_kwargs = {**cookie_kwargs, "headers": headers}

        async with self._http_session.post(url, json=body, **request_kwargs) as response:
            text = await response.text()
            if response.status == 403:
                raise AuthenticationError("Failed to authenticate with MyAnonamouse")
            if not response.ok:
                logger.error(
                    "MamService: search failed",
                    status=response.status,
                    reason=response.reason,
                    body=text,
                )
                raise SearchError(f"MAM query failed: {response.status}")
            if text.strip().startswith("Error"):
                raise SearchError(text.strip())
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                # Log a short preview so we can see if we got an HTML login page or other unexpected content.
                logger.warning(
                    "MamService: unable to decode response",
                    body_preview=text[:500],
                )
                return []

        if isinstance(payload, dict) and "error" in payload:
            error_message = str(payload["error"])
            if error_message.lower().startswith("nothing returned"):
                return []
            raise SearchError(error_message)

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            logger.warning("MamService: unexpected payload structure", payload=payload)
            return []
        return self._normalize_results(data, sanitized_query or query)

    def _cookie_kwargs(self) -> dict:
        raw = self._settings.mam_session_id
        if "=" in raw or ";" in raw:
            return {"headers": {"Cookie": raw}}
        return {"cookies": {"mam_id": raw}}

    def _normalize_results(
        self, payload: list[dict[str, Any]], fallback: str
    ) -> list[MamSearchResult]:
        normalized: list[MamSearchResult] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            result = self._normalize_result(raw, fallback)
            if result:
                normalized.append(result)
        return normalized

    def _normalize_result(
        self, raw: dict[str, Any], fallback: str
    ) -> MamSearchResult | None:
        guid = _determine_guid(raw)
        title = self._decorate_title(raw, fallback)
        publish_date = _coerce_datetime(raw.get("added") or raw.get("timestamp"))
        size = _extract_size(raw)
        seeders = _extract_seeders(raw)
        leechers = _extract_leechers(raw)
        flags = _flags_from_result(raw)
        torrent_id = self._extract_torrent_id(raw)
        dl_hash = _coerce_dl_hash(raw)
        link = self._build_download_link(torrent_id)
        details = self._build_details_link(torrent_id)
        peers = seeders + leechers
        download_volume_factor = 0.0 if self._is_free(flags, raw) else 1.0
        return MamSearchResult(
            guid=guid,
            title=title,
            link=link,
            details=details,
            size=size,
            seeders=seeders,
            leechers=leechers,
            peers=peers,
            publish_date=publish_date,
            download_volume_factor=download_volume_factor,
            minimum_seed_time=self.MINIMUM_SEED_TIME,
            flags=flags,
            raw={**raw, "dl_hash": dl_hash} if dl_hash else raw,
        )

    def _decorate_title(self, raw: dict[str, Any], fallback: str) -> str:
        base = _coerce_title(raw, fallback)
        authors = _parse_people(raw.get("author_info"))
        if authors:
            base = f"{base} - {', '.join(authors)}"
        markers: list[str] = []
        language = _first_value(
            raw, ("language", "lang", "lang_name", "language_name")
        )
        if language:
            markers.append(language.upper())
        filetype = _first_value(
            raw,
            ("filetype", "file_type", "torFileType", "format", "container"),
        )
        if filetype:
            markers.append(filetype.upper())
        if raw.get("vip") in (1, "1", True):
            markers.append("VIP")
        if raw.get("fl_vip") in (1, "1", True):
            markers.append("FL-VIP")
        if markers:
            marker_text = "".join(f"[{marker}]" for marker in markers)
            base = f"{base} {marker_text}".strip()
        return base

    def _extract_torrent_id(self, raw: dict[str, Any]) -> str | None:
        for key in ("id", "tid", "tor_id", "torrent_id"):
            value = raw.get(key)
            if value is not None:
                return str(value)
        return None

    def _build_details_link(self, torrent_id: str | None) -> str:
        if not torrent_id:
            return self._settings.mam_base_url.rstrip("/")
        return f"{self._settings.mam_base_url.rstrip('/')}/t/{torrent_id}"

    def _build_download_link(self, torrent_id: str | None) -> str:
        if not torrent_id:
            return self._settings.mam_base_url.rstrip("/")
        endpoint = self._settings.torrent_download_endpoint.format(id=torrent_id)
        return urljoin(self._settings.mam_base_url, endpoint)

    async def download_torrent(self, torrent_id: str | int) -> bytes:
        """Download the .torrent file for a given torrent ID."""
        if self._settings.use_mock_data:
            return b"d8:announce35:udp://tracker.openbittorrent.com:8013:creation datei1327049827e4:infod6:lengthi123456789e4:name14:Mock Audiobook12:piece lengthi262144e6:pieces20:01234567890123456789ee"

        # Try the common MAM download endpoints in order (mirrors audiofinder/Jackett patterns).
        endpoints = [
            f"/tor/download.php/{getattr(self, '_last_dl_hash', None)}"
            if getattr(self, "_last_dl_hash", None)
            else None,
            self._settings.torrent_download_endpoint.format(id=torrent_id),  # default: /torrents.php?action=download&id={id}
            f"/tor/download.php?id={torrent_id}",
            f"/tor/download.php?tid={torrent_id}",
        ]
        endpoints = [e for e in endpoints if e]  # drop None

        cookie_kwargs = self._cookie_kwargs()
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/x-bittorrent, */*",
            "Origin": self._settings.mam_base_url.rstrip("/"),
            "Referer": f"{self._settings.mam_base_url.rstrip('/')}/",
        }
        if "headers" in cookie_kwargs:
            headers.update(cookie_kwargs.get("headers", {}))
            cookie_kwargs = {k: v for k, v in cookie_kwargs.items() if k != "headers"}

        last_error: Exception | None = None
        for endpoint in endpoints:
            url = urljoin(self._settings.mam_base_url, endpoint)
            logger.debug("MamClient: downloading torrent", torrent_id=torrent_id, url=url)
            try:
                async with self._http_session.get(url, headers=headers, **cookie_kwargs) as response:
                    if response.status == 403:
                        last_error = AuthenticationError("Torrent download forbidden (check session id)")
                        continue
                    if not response.ok:
                        text = await response.text()
                        last_error = RuntimeError(
                            f"Failed to download torrent {torrent_id}: {response.status} {text}"
                        )
                        continue
                    return await response.read()
            except Exception as exc:
                last_error = exc
                continue

        if last_error:
            raise last_error
        raise RuntimeError("Failed to download torrent: no valid endpoint")

    @staticmethod
    def _is_free(flags: list[str], raw: dict[str, Any]) -> bool:
        if any(flag in {"free", "freeleech", "personal_freeleech"} for flag in flags):
            return True
        return raw.get("personal_freeleech") in (1, "1", True)
