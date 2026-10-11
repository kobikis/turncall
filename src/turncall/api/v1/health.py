"""Health check endpoint."""

from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from loguru import logger
from sqlalchemy import text

from turncall.services import drain
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
            await conn.execute(text("SELECT 1"))
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
    """Kubernetes readiness probe - checks if the service can serve traffic.

    503 `draining` from the first SIGTERM on (#199): the process keeps serving
    the calls it has, but new ones belong on another replica.
    """
    if drain.is_draining():
        from fastapi.responses import JSONResponse

        return JSONResponse(  # type: ignore[return-value]
            status_code=503,
            content={"status": "draining", "active_calls": drain.active_calls()},
        )
    try:
        engine = get_engine()
        async with engine.connect() as conn:
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
    """Kubernetes liveness probe - checks if the process is alive.

    Stays 200 while draining: a draining process is healthy, and a liveness
    failure would get it killed with its calls still on it.
    """
    return {"status": "alive"}


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    """Prometheus exposition: live voice calls on this process (#199).

    Per process, as every gauge is: scrape each replica, not a load-balanced
    address. The input to an autoscaler, and to a drain's progress.
    """
    return (
        "# HELP turncall_active_calls Live voice calls on this process.\n"
        "# TYPE turncall_active_calls gauge\n"
        f"turncall_active_calls {drain.active_calls()}\n"
    )
