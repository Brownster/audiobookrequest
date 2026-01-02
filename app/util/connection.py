import aiohttp
from typing import Optional


class HTTPSessionManager:
    """
    Singleton HTTP session manager to prevent resource leaks.
    Reuses a single ClientSession instead of creating one per request.
    """
    _session: Optional[aiohttp.ClientSession] = None
    _timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=60, connect=10)

    @classmethod
    async def get_session(cls) -> aiohttp.ClientSession:
        """Get or create the shared HTTP session."""
        if cls._session is None or cls._session.closed:
            cls._session = aiohttp.ClientSession(timeout=cls._timeout)
        return cls._session

    @classmethod
    async def close(cls) -> None:
        """Close the shared HTTP session (call on app shutdown)."""
        if cls._session is not None and not cls._session.closed:
            await cls._session.close()
            cls._session = None


async def get_connection():
    """FastAPI dependency for HTTP session - uses shared session manager."""
    session = await HTTPSessionManager.get_session()
    yield session
    # Don't close - session is managed by HTTPSessionManager
