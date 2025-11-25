from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.util.log import logger


def _clean_title(raw: str) -> str:
    title = raw or ""
    # Drop trailing flags like [M4B][FLAC] etc.
    title = re.sub(r"\[[^\]]+\]", "", title)
    # Collapse whitespace
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _parse_list_field(val: Any) -> list[str]:
    if not val:
        return []
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    if isinstance(val, dict):
        return [str(v).strip() for v in val.values() if str(v).strip()]
    try:
        import json

        parsed = json.loads(val)
        return _parse_list_field(parsed)
    except Exception:
        if isinstance(val, str) and val.strip():
            return [val.strip()]
    return []


@dataclass
class NormalizedMAM:
    title: str
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    size: float = 0.0
    seeders: int = 0
    leechers: int = 0
    publish_date: str | None = None
    filetype: str | None = None
    cover_image: str | None = None
    subtitle: str | None = None
    source: Any = None
    label: str = "MAM"


def normalize_mam_results(results: Iterable[Any]) -> list[NormalizedMAM]:
    normalized: list[NormalizedMAM] = []
    for r in results:
        try:
            raw_title = getattr(r, "title", "") or getattr(r, "name", "")
            title = _clean_title(raw_title)
            authors = _parse_list_field(getattr(r, "raw", {}).get("author_info"))
            narrators = _parse_list_field(getattr(r, "raw", {}).get("narrator_info"))
            flags = getattr(r, "flags", []) or []
            size = float(getattr(r, "size", 0) or 0)
            seeders = int(getattr(r, "seeders", 0) or 0)
            leechers = int(getattr(r, "leechers", 0) or 0)
            publish_date = getattr(r, "publish_date", None)
            filetype = getattr(r, "raw", {}).get("filetype") or getattr(r, "filetype", None)
            normalized.append(
                NormalizedMAM(
                    title=title,
                    authors=authors,
                    narrators=narrators,
                    flags=list(flags),
                    size=size,
                    seeders=seeders,
                    leechers=leechers,
                    publish_date=publish_date,
                    filetype=filetype,
                    source=r,
                )
            )
        except Exception as e:
            logger.debug("Failed to normalize MAM result", error=str(e))
    return normalized
