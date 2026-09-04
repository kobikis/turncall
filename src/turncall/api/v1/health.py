"""Health check endpoint."""

from typing import Any

from fastapi import APIRouter
from loguru import logger

from turncall.storage.database import get_engine
from turncall.storage.redis import get_redis

router = APIRouter(tags=["health"])



@router.get("/health")
async def health_check() -> dict[str, Any]:
    """Check service health including database and Redis connectivity."""
    checks: dict[str, str] = {}

    # Database check
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(conn.default_isolation_level)  # type: ignore[arg-type]
        checks["database"] = "healthy"
    except Exception as exc:
        logger.warning("health_check_db_failed", error=str(exc))
        checks["database"] = "unhealthy"

    # Redis check
    try:
        redis = get_redis()
        await redis.ping()
        checks["redis"] = "healthy"
    except Exception as exc:
        logger.warning("health_check_redis_failed", error=str(exc))
        checks["redis"] = "unhealthy"

    all_healthy = all(v == "healthy" for v in checks.values())

    return {
        "status": "healthy" if all_healthy else "degraded",
        "checks": checks,
        "version": "0.1.0",
    }


@router.get("/ready")
async def readiness_check() -> dict[str, str]:
    """Kubernetes readiness probe - checks if the service can serve traffic."""
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            from sqlalchemy import text

            await conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:
        from fastapi.responses import JSONResponse

        return JSONResponse(  # type: ignore[return-value]
            status_code=503,
            content={"status": "not_ready"},
        )


@router.get("/live")
async def liveness_check() -> dict[str, str]:
    """Kubernetes liveness probe - checks if the process is alive."""
    return {"status": "alive"}
