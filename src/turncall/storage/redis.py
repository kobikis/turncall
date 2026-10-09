"""Redis connection management."""

from redis.asyncio import ConnectionPool, Redis

from turncall.config.settings import RedisSettings

_pool: ConnectionPool | None = None
_client: Redis | None = None
_url: str | None = None


async def init_redis(settings: RedisSettings) -> None:
    """Initialize Redis connection pool."""
    global _pool, _client, _url
    _url = str(settings.url)
    _pool = ConnectionPool.from_url(
        str(settings.url),
        max_connections=settings.max_connections,
        decode_responses=True,
    )
    _client = Redis(connection_pool=_pool)


async def close_redis() -> None:
    """Close Redis connections."""
    global _pool, _client, _url
    _url = None
    if _client is not None:
        await _client.aclose()
        _client = None
    if _pool is not None:
        await _pool.aclose()
        _pool = None


def get_redis() -> Redis:
    """Get the Redis client."""
    if _client is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    return _client


def dedicated_client() -> Redis:
    """A client with a connection of its own, outside the shared pool — for a
    subscription, which holds its connection for as long as it lives. The
    caller closes it."""
    if _url is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    return Redis.from_url(_url, decode_responses=True)
