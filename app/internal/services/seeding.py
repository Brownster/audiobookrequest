from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from app.internal.models import DownloadJob
# We'll define a Protocol or simple class for Settings to avoid circular imports or complex dependencies
from typing import Protocol

class SeedingSettings(Protocol):
    seed_target_hours: int
    qbittorrent_seed_ratio: float | None
    qbittorrent_seed_time: int | None


@dataclass(frozen=True)
class TorrentSeedConfiguration:
    """Represents how long torrents should seed and related share limits."""

    required_seed_seconds: int
    ratio_limit: float | None
    seeding_time_limit: int | None

    def to_record(self) -> dict[str, Any]:
        return {
            "required_seed_seconds": self.required_seed_seconds,
            "ratio_limit": self.ratio_limit,
            "seeding_time_limit": self.seeding_time_limit,
        }

    @classmethod
    def from_record(
        cls, record: Mapping[str, Any] | None
    ) -> "TorrentSeedConfiguration | None":
        if not record:
            return None
        ratio = record.get("ratio_limit")
        if ratio is not None:
            ratio = float(ratio)
        seeding_time = record.get("seeding_time_limit")
        if seeding_time is not None:
            seeding_time = int(seeding_time)
        return cls(
            required_seed_seconds=int(record.get("required_seed_seconds", 0)),
            ratio_limit=ratio,
            seeding_time_limit=seeding_time,
        )


def build_seed_configuration(
    job: DownloadJob, settings: SeedingSettings, release_min_seed_time: int = 0
) -> TorrentSeedConfiguration:
    """Determine the seed policy to enforce for a torrent."""

    global_required = int(settings.seed_target_hours * 3600)
    required_seconds = max(global_required, release_min_seed_time)

    ratio_limit = settings.qbittorrent_seed_ratio
    required_minutes = math.ceil(required_seconds / 60) if required_seconds else 0

    seeding_time_limit = settings.qbittorrent_seed_time
    if required_minutes:
        if seeding_time_limit is None:
            seeding_time_limit = required_minutes
        else:
            seeding_time_limit = max(seeding_time_limit, required_minutes)

    return TorrentSeedConfiguration(
        required_seed_seconds=required_seconds,
        ratio_limit=ratio_limit,
        seeding_time_limit=seeding_time_limit,
    )
