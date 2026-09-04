"""FastAPI application factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from turncall.api.errors import register_error_handlers
from turncall.api.middleware import RequestIdMiddleware, TimingMiddleware
from turncall.api.v1.health import router as health_router
from turncall.config.logging import setup_logging
from turncall.config.settings import Settings, get_settings
from turncall.storage.database import close_database, init_database
from turncall.storage.redis import close_redis, init_redis


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown."""
    settings = get_settings()

    setup_logging(
        log_level=settings.server.log_level,
        json_output=settings.is_production,
    )

    logger.info(
        "starting_turncall",
        environment=settings.environment,
        version="0.1.0",
    )

    # OpenTelemetry tracing (ADR-0010) — set up once; per-call spans are enabled
    # on the PipelineTask. No-ops cleanly if disabled / no OTLP endpoint in prod.
    from turncall.orchestrator.telemetry import init_tracing

    init_tracing(
        enabled=settings.pipecat.enable_tracing,
        service_name=settings.pipecat.otel_service_name,
        is_production=settings.is_production,
    )

    # Initialize infrastructure
    await init_database(settings.database)
    await init_redis(settings.redis)

    # Log all agent configs on startup
    try:
        from turncall.storage.database import create_session_factory, get_engine

        session_factory = create_session_factory(get_engine())
        async with session_factory() as session:
            from sqlalchemy import select

            from turncall.storage.models import AgentRow

            result = await session.execute(
                select(AgentRow).order_by(AgentRow.created_at.desc())
            )
            agents = result.scalars().all()
            if agents:
                logger.info(f"Loaded {len(agents)} agent(s):")
                for a in agents:
                    cfg = a.config_blob or {}
                    stt = cfg.get("stt", {})
                    llm = cfg.get("llm", {})
                    tts = cfg.get("tts", {})
                    logger.info(
                        f"  [{a.state}] {a.name} v{a.version}"
                        f" | STT: {stt.get('provider')}/{stt.get('model')}"
                        f" | LLM: {llm.get('provider')}/{llm.get('model')}"
                        f" | TTS: {tts.get('provider')}/{tts.get('voice')}"
                    )
            else:
                logger.info("No agents configured yet.")
    except Exception:
        logger.warning("Could not load agents on startup")

    logger.info("turncall_started")

    # Start background session cleanup task
    import asyncio

    async def _cleanup_expired_sessions() -> None:
        """Periodically expire stale SMS sessions."""
        from turncall.storage.database import create_session_factory, get_engine
        from turncall.storage.repositories import sms_session_repo

        session_factory = create_session_factory(get_engine())
        while True:
            try:
                await asyncio.sleep(900)  # 15 minutes
                async with session_factory() as db:
                    count = await sms_session_repo.expire_stale_sessions(db)
                    await db.commit()
                    if count > 0:
                        logger.info(f"Expired {count} stale SMS session(s)")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("session_cleanup_error")

    cleanup_task = asyncio.create_task(_cleanup_expired_sessions())

    async def _purge_soft_deleted_projects() -> None:
        """Periodically hard-delete projects soft-deleted past the retention
        window (ADR-0011) — the FK cascade reclaims each tree."""
        from turncall.config import get_settings
        from turncall.services.project_deletion import purge_soft_deleted_projects
        from turncall.storage.database import create_session_factory, get_engine

        settings = get_settings()
        session_factory = create_session_factory(get_engine())
        while True:
            try:
                await asyncio.sleep(3600)  # hourly (retention is in days)
                count = await purge_soft_deleted_projects(
                    session_factory,
                    settings,
                    retention_days=settings.project_purge_retention_days,
                )
                if count > 0:
                    logger.info(f"Purged {count} soft-deleted project(s)")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("project_purge_error")

    purge_task = asyncio.create_task(_purge_soft_deleted_projects())

    yield

    cleanup_task.cancel()
    purge_task.cancel()

    # Shutdown
    logger.info("shutting_down_turncall")

    # Close WhatsApp aiohttp session
    from turncall.webhooks.whatsapp_handlers import close_http_session

    await close_http_session()

    # Close the shared httpx client (pooled connections)
    from turncall.adapters.http_client import close_http_client

    await close_http_client()

    # Close the shared aiohttp session (ElevenLabs STT + avatar services)
    from turncall.adapters.aiohttp_client import close_aiohttp_session

    await close_aiohttp_session()

    await close_redis()
    await close_database()
    logger.info("turncall_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title="TurnCall",
        description="Production voice agent platform API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
    )

    # Middleware (order matters: first added = outermost)
    app.add_middleware(TimingMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if not settings.is_production else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Error handlers
    register_error_handlers(app)

    # Routers
    app.include_router(health_router)

    # V1 API routers
    from turncall.api.v1.agents import router as agents_router
    from turncall.api.v1.api_keys import router as api_keys_router
    from turncall.api.v1.call_control import router as call_control_router
    from turncall.api.v1.calls import router as calls_router
    from turncall.api.v1.calls_outbound import router as calls_outbound_router
    from turncall.api.v1.phone_numbers import router as phone_numbers_router
    from turncall.api.v1.projects import router as projects_router

    app.include_router(projects_router, prefix="/v1")
    app.include_router(api_keys_router, prefix="/v1")
    app.include_router(agents_router, prefix="/v1")
    app.include_router(calls_router, prefix="/v1")
    app.include_router(calls_outbound_router, prefix="/v1")
    app.include_router(call_control_router, prefix="/v1")
    app.include_router(phone_numbers_router, prefix="/v1")

    from turncall.api.v1.chat import router as chat_router

    app.include_router(chat_router, prefix="/v1")

    from turncall.api.v1.knowledge import agent_kb_router as agent_kb_router
    from turncall.api.v1.knowledge import router as knowledge_router

    app.include_router(knowledge_router, prefix="/v1")
    app.include_router(agent_kb_router, prefix="/v1")

    from turncall.api.v1.takeaways import router as takeaways_router

    app.include_router(takeaways_router, prefix="/v1")

    from turncall.api.v1.testing import router as test_suites_router
    from turncall.api.v1.testing import test_runs_router
    from turncall.api.v1.tools import router as tools_router
    from turncall.api.v1.webhooks import router as webhooks_router

    app.include_router(tools_router, prefix="/v1")
    app.include_router(webhooks_router, prefix="/v1")
    app.include_router(test_suites_router, prefix="/v1")
    app.include_router(test_runs_router, prefix="/v1")

    from turncall.api.v1.webrtc import router as webrtc_router

    app.include_router(webrtc_router, prefix="/v1")

    # Twilio webhooks (no /v1 prefix)
    from turncall.webhooks.media_stream import router as media_stream_router
    from turncall.webhooks.twilio_handlers import router as twilio_webhook_router

    app.include_router(twilio_webhook_router)
    app.include_router(media_stream_router)

    # WhatsApp webhooks (no /v1 prefix)
    from turncall.webhooks.whatsapp_handlers import router as whatsapp_webhook_router

    app.include_router(whatsapp_webhook_router)

    return app
