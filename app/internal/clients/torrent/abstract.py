from abc import ABC, abstractmethod
from typing import Any, Iterable, Optional

class AbstractTorrentClient(ABC):
    @abstractmethod
    async def add_torrent(self, torrent_bytes: bytes, **kwargs) -> dict | None:
        """
        Add a torrent to the client.
        
        Args:
            torrent_bytes: The raw .torrent file content.
            **kwargs: Client-specific options.
            
        Returns:
            A dictionary containing torrent info (e.g. id, hash, name) if available immediately,
            otherwise None.
        """
        pass

    @abstractmethod
    async def get_torrents(self, hashes: Iterable[str]) -> dict[str, dict[str, Any]]:
        """
        Get information about specific torrents.
        
        Args:
            hashes: A list of torrent hashes to query.
            
        Returns:
            A dictionary mapping hash strings to torrent info dictionaries.
        """
        pass

    @abstractmethod
    async def remove_torrent(self, hash_string: str, delete_data: bool = False) -> None:
        """
        Remove a torrent from the client.
        
        Args:
            hash_string: The hash of the torrent to remove.
            delete_data: Whether to delete the downloaded data as well.
        """
        pass

    @abstractmethod
    async def test_connection(self) -> None:
        """
        Test connectivity to the torrent client.
        Raises an exception if connection fails.
        """
        pass

    @abstractmethod
    async def set_share_limits(
        self,
        hash_string: str,
        *,
        ratio_limit: float | None = None,
        seeding_time_limit: int | None = None,
    ) -> None:
        """Apply ratio/time limits when supported by the client."""
        pass
