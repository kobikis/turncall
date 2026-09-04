"""Redis connection management."""

from redis.asyncio import ConnectionPool, Redis

from turncall.config.settings import RedisSettings

_pool: ConnectionPool | None = None
_client: Redis | None = None


async def init_redis(settings: RedisSettings) -> None:
    """Initialize Redis connection pool."""
    global _pool, _client
    _pool = ConnectionPool.from_url(
        str(settings.url),
        max_connections=settings.max_connections,
        decode_responses=True,
    )
    _client = Redis(connection_pool=_pool)


async def close_redis() -> None:
    """Close Redis connections."""
    global _pool, _client
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
