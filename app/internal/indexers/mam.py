import json
from typing import Any
from urllib.parse import urlencode, urljoin

from app.internal.clients.mam import MamClientSettings, MyAnonamouseClient

from app.internal.indexers.abstract import (
    AbstractIndexer,
    SessionContainer,
)
from app.internal.indexers.configuration import (
    Configurations,
    IndexerConfiguration,
    ValuedConfigurations,
)
from app.internal.models import (
    BookRequest,
    ProwlarrSource,
)
from app.util.log import logger


class MamConfigurations(Configurations):
    mam_session_id: IndexerConfiguration[str] = IndexerConfiguration(
        type=str,
        display_name="MAM Session ID",
        required=True,
    )
    download_client: IndexerConfiguration[str] = IndexerConfiguration(
        type=str,
        display_name="Download Client (transmission/qbittorrent)",
        default="transmission",
        required=True,
    )
    transmission_url: IndexerConfiguration[str] = IndexerConfiguration(
        type=str,
        display_name="Transmission URL",
        default="http://transmission:9091/transmission/rpc",
        required=False,
    )
    transmission_username: IndexerConfiguration[str] = IndexerConfiguration(
        type=str,
        display_name="Transmission Username",
        required=False,
    )
    transmission_password: IndexerConfiguration[str] = IndexerConfiguration(
        type=str,
        display_name="Transmission Password",
        required=False,
    )
    qbittorrent_url: IndexerConfiguration[str] = IndexerConfiguration(
        type=str,
        display_name="qBittorrent URL",
        default="http://qbittorrent:8080",
        required=False,
    )
    qbittorrent_username: IndexerConfiguration[str] = IndexerConfiguration(
        type=str,
        display_name="qBittorrent Username",
        required=False,
    )
    qbittorrent_password: IndexerConfiguration[str] = IndexerConfiguration(
        type=str,
        display_name="qBittorrent Password",
        required=False,
    )
    seed_target_hours: IndexerConfiguration[int] = IndexerConfiguration(
        type=int,
        display_name="Seed Target (Hours)",
        default=72,
        required=True,
    )
    qbittorrent_seed_ratio: IndexerConfiguration[float] = IndexerConfiguration(
        type=float,
        display_name="qBittorrent Seed Ratio Limit",
        required=False,
    )
    qbittorrent_seed_time: IndexerConfiguration[int] = IndexerConfiguration(
        type=int,
        display_name="qBittorrent Seed Time Limit (Minutes)",
        required=False,
    )
    qbittorrent_remote_path_prefix: IndexerConfiguration[str] = IndexerConfiguration(
        type=str,
        display_name="qBittorrent Remote Download Path",
        required=False,
        default="",
    )
    qbittorrent_local_path_prefix: IndexerConfiguration[str] = IndexerConfiguration(
        type=str,
        display_name="qBittorrent Local Path Prefix",
        required=False,
        default="",
    )
    use_mock_data: IndexerConfiguration[bool] = IndexerConfiguration(
        type=bool,
        display_name="Use Mock Data (Dev Only)",
        default=False,
        required=False,
    )


class ValuedMamConfigurations(ValuedConfigurations):
    mam_session_id: str
    download_client: str
    transmission_url: str | None
    transmission_username: str | None
    transmission_password: str | None
    qbittorrent_url: str | None
    qbittorrent_username: str | None
    qbittorrent_password: str | None
    seed_target_hours: int
    qbittorrent_seed_ratio: float | None
    qbittorrent_seed_time: int | None
    qbittorrent_remote_path_prefix: str | None
    qbittorrent_local_path_prefix: str | None
    use_mock_data: bool | None


class MamIndexer(AbstractIndexer[MamConfigurations]):
    name = "MyAnonamouse"

    def __init__(self):
        # keep results scoped per instance/run to avoid stale cross-request data
        self.results: dict[str, dict[str, Any]] = {}

    @staticmethod
    async def get_configurations(
        container: SessionContainer,
    ) -> MamConfigurations:
        return MamConfigurations()

    async def setup(
        self,
        request: BookRequest,
        container: SessionContainer,
        configurations: ValuedMamConfigurations,
    ):
        # reset results each time so we don't leak between requests
        self.results = {}
        if not await self.is_enabled(container, configurations):
            return

        settings = MamClientSettings(
            mam_session_id=configurations.mam_session_id,
            use_mock_data=configurations.use_mock_data or False,
        )
        client = MyAnonamouseClient(container.client_session, settings)
        
        try:
            # MAM audiobook category is 13, which is the default in MamClientSettings
            # but we can be explicit if needed.
            results = await client.search(request.title, limit=100)
        except Exception as e:
            logger.error("Mam: Search failed", error=str(e))
            return

        for result in results:
            # MamIndexer expects raw dicts keyed by ID
            # The raw dict from MamSearchResult.raw should be compatible
            # We might need to ensure 'id' is present and correct type if it was coerced
            raw = result.raw
            # Ensure ID is present as it's used as key
            if "id" not in raw and "id" in result.raw:
                 # It should be there.
                 pass
            
            # The original code used result["id"]
            self.results[str(raw.get("id"))] = raw
            
        logger.info("Mam: Retrieved results", results_amount=len(self.results))

    async def is_matching_source(
        self,
        source: ProwlarrSource,
        container: SessionContainer,
    ):
        return source.info_url is not None and source.info_url.startswith(
            "https://www.myanonamouse.net/t/"
        )

    async def edit_source_metadata(
        self,
        source: ProwlarrSource,
        container: SessionContainer,
    ):
        mam_id = source.guid.split("-")[-1]
        result = self.results.get(mam_id)
        if result is None:
            return

        # response type of authors and narrators is a stringified json object
        source.book_metadata.authors = list(
            json.loads(result.get("author_info", "{}")).values()
        )

        source.book_metadata.narrators = list(
            json.loads(result.get("narrator_info", "{}")).values()
        )

        indexer_flags: set[str] = set(source.indexer_flags)
        if result["personal_freeleech"] == 1:
            indexer_flags.add("personal_freeleech")
            indexer_flags.add("freeleech")
        if result["free"] == 1:
            indexer_flags.add("free")
            indexer_flags.add("freeleech")
        if result["fl_vip"] == 1:
            indexer_flags.add("fl_vip")
            indexer_flags.add("freeleech")
        if result["vip"] == 1:
            indexer_flags.add("vip")

        source.indexer_flags = list(indexer_flags)

        source.book_metadata.filetype = result["filetype"]
