from __future__ import annotations

import base64
from typing import Any, Dict, Iterable, Optional

from aiohttp import BasicAuth, ClientSession

from app.util.log import logger
from app.internal.clients.torrent.abstract import AbstractTorrentClient


class TransmissionError(RuntimeError):
    pass


class TransmissionClient(AbstractTorrentClient):
    """Async wrapper around the Transmission RPC API."""

    def __init__(
        self,
        session: ClientSession,
        rpc_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self._session = session
        self._rpc_url = rpc_url
        self._auth = BasicAuth(username, password) if username else None
        self._session_id: Optional[str] = None

    async def _rpc(self, method: str, arguments: Optional[dict] = None) -> dict:
        payload = {"method": method, "arguments": arguments or {}}
        headers: Dict[str, str] = {}
        if self._session_id:
            headers["X-Transmission-Session-Id"] = self._session_id

        for attempt in range(2):
            async with self._session.post(
                self._rpc_url,
                json=payload,
                headers=headers,
                auth=self._auth,
            ) as response:
                if response.status == 409:
                    self._session_id = response.headers.get("X-Transmission-Session-Id")
                    headers["X-Transmission-Session-Id"] = self._session_id or ""
                    continue
                if not response.ok:
                    text = await response.text()
                    raise TransmissionError(
                        f"RPC {method} failed: {response.status} {response.reason} {text}"
                    )
                data = await response.json()
                if data.get("result") != "success":
                    raise TransmissionError(
                        f"RPC {method} failed: {data.get('result')}"
                    )
                return data
        raise TransmissionError("Unable to negotiate Transmission session id")

    async def add_torrent(self, torrent_bytes: bytes, **kwargs) -> dict:
        arguments = {
            "metainfo": base64.b64encode(torrent_bytes).decode(),
        }
        # Handle potential kwargs if needed, e.g. download_dir
        if "download_dir" in kwargs:
            arguments["download-dir"] = kwargs["download_dir"]

        data = await self._rpc("torrent-add", arguments)
        result = data["arguments"].get("torrent-added") or data["arguments"].get(
            "torrent-duplicate"
        )
        if not result:
            raise TransmissionError("Failed to add torrent, Transmission returned empty data")
        logger.info(
            "Transmission: torrent registered",
            torrent_id=result.get("id"),
            hash=result.get("hashString"),
        )
        return result

    async def get_torrents(self, hashes: Iterable[str]) -> dict[str, dict[str, Any]]:
        hash_list = list(hashes)
        if not hash_list:
            return {}
        arguments = {
            "fields": [
                "id",
                "name",
                "hashString",
                "status",
                "uploadRatio",
                "secondsSeeding",
                "percentDone",
                "rateDownload",
                "rateUpload",
                "uploadedEver",
                "downloadDir",
                "leftUntilDone",
                "eta",
                "isFinished",
                "addedDate",
                "doneDate",
                "files",
            ],
            "ids": hash_list,
        }
        data = await self._rpc("torrent-get", arguments)
        torrents = data["arguments"].get("torrents", [])
        return {tor["hashString"]: tor for tor in torrents}

    async def remove_torrent(self, hash_string: str, delete_data: bool = False) -> None:
        await self._rpc(
            "torrent-remove",
            arguments={"ids": [hash_string], "delete-local-data": delete_data},
        )
        logger.info("Transmission: torrent removed", hash=hash_string)

    async def test_connection(self) -> None:
        await self._rpc("session-get")

    async def set_share_limits(
        self,
        hash_string: str,
        *,
        ratio_limit: float | None = None,
        seeding_time_limit: int | None = None,
    ) -> None:
        # Transmission does not support per-torrent share limits via RPC in this client; noop.
        return
