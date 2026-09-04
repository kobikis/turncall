"""Call management endpoints."""

from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import Response
from loguru import logger

from turncall.adapters.storage import create_storage_adapter
from turncall.api.deps import DbSession
from turncall.api.errors import NotFoundError
from turncall.api.responses import ok, paginated
from turncall.api.v1.schemas.calls import CallEventResponse, CallResponse
from turncall.auth import Auth
from turncall.config.settings import get_settings
from turncall.storage.repositories import call_repo

router = APIRouter(prefix="/calls", tags=["calls"])


@router.get("/{call_id}")
async def get_call(
    call_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Get a call by ID."""
    row = await call_repo.get_call_by_id(session, call_id, project_id=auth.project_id)
    if row is None:
        raise NotFoundError("Call", str(call_id))
    return ok(CallResponse.from_row(row))


@router.get("")
async def list_calls(
    auth: Auth,
    session: DbSession,
    status: str | None = None,
    direction: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict:
    """List calls for the authenticated project."""
    offset = (page - 1) * limit
    rows = await call_repo.list_calls(
        session,
        auth.project_id,
        status=status,
        direction=direction,
        limit=limit,
        offset=offset,
    )
    total = await call_repo.count_calls(session, auth.project_id, status=status)
    return paginated(
        data=[CallResponse.from_row(r) for r in rows],
        total=total,
        page=page,
        limit=limit,
    )


@router.get("/{call_id}/events")
async def list_call_events(
    call_id: UUID,
    auth: Auth,
    session: DbSession,
    event_type: str | None = None,
    page: int = 1,
    limit: int = 200,
) -> dict:
    """List events for a call."""
    # Verify call belongs to project
    call = await call_repo.get_call_by_id(session, call_id, project_id=auth.project_id)
    if call is None:
        raise NotFoundError("Call", str(call_id))

    offset = (page - 1) * limit
    events = await call_repo.list_call_events(
        session, call_id, event_type=event_type, limit=limit, offset=offset
    )
    return ok([CallEventResponse.model_validate(e) for e in events])


@router.get("/{call_id}/transcript")
async def get_call_transcript(
    call_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Get the full transcript for a call.

    Extracts transcript entries from call events and returns them
    in chronological order.
    """
    call = await call_repo.get_call_by_id(session, call_id, project_id=auth.project_id)
    if call is None:
        raise NotFoundError("Call", str(call_id))

    from turncall.domain.enums import CallEventType

    events = await call_repo.list_call_events(
        session,
        call_id,
        event_type=CallEventType.TRANSCRIPT_FINAL,
        limit=1000,
    )

    transcript_entries = [
        {
            "role": e.payload.get("role"),  # "customer" | "assistant"
            "text": e.payload.get("text", ""),
            "timestamp": e.internal_timestamp.isoformat(),
            "sequence": e.sequence_number,
        }
        for e in events
    ]

    return ok(
        {
            "call_id": str(call_id),
            "entries": transcript_entries,
            "count": len(transcript_entries),
        }
    )


@router.get("/{call_id}/recording")
async def get_call_recording(
    call_id: UUID,
    auth: Auth,
    session: DbSession,
) -> Response:
    """Stream the call recording (WAV) from object storage."""
    call = await call_repo.get_call_by_id(session, call_id, project_id=auth.project_id)
    if call is None:
        raise NotFoundError("Call", str(call_id))
    url = call.recording_url or ""
    if "recordings/" not in url:
        raise NotFoundError("Recording", str(call_id))

    settings = get_settings()
    storage = create_storage_adapter(
        backend=settings.storage.backend,
        local_path=settings.storage.local_path,
        s3_bucket=settings.storage.s3_bucket,
        aws_region=settings.storage.aws_region,
    )
    # Both backends embed the storage key in the URL. Parse s3:// properly —
    # a substring search would match inside the bucket name ("call-recordings").
    if url.startswith("s3://"):
        key = url.split("/", 3)[3]  # s3://<bucket>/<key>
    else:
        key = url[url.rindex("recordings/") :]  # local path ends with the key
    try:
        audio = await storage.download(key)
    except Exception as exc:
        logger.warning(
            "recording_download_failed: call={call_id} key={key}: {err}",
            call_id=str(call_id),
            key=key,
            err=exc,
        )
        raise NotFoundError("Recording", str(call_id)) from exc
    # ponytail: whole file in memory — fine for call-length WAVs; stream if hour-long calls appear
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={"Content-Disposition": f'inline; filename="{call_id}.wav"'},
    )


@router.get("/{call_id}/analysis")
async def get_call_analysis(
    call_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Get post-call analysis results."""
    from turncall.api.v1.schemas.calls import AnalysisResponse

    call = await call_repo.get_call_by_id(session, call_id, project_id=auth.project_id)
    if call is None:
        raise NotFoundError("Call", str(call_id))

    if call.analysis_json is not None:
        return ok(
            AnalysisResponse(
                call_id=call_id, status="completed", analysis=call.analysis_json
            )
        )

    # Check if analysis is configured for this call's agent
    if call.active_agent_id:
        from turncall.storage.repositories import agent_repo

        agent = await agent_repo.get_agent_by_id(session, call.active_agent_id)
        if agent:
            analysis_cfg = agent.config_blob.get("analysis", {})
            if not analysis_cfg.get("enabled", True):
                return ok(AnalysisResponse(call_id=call_id, status="not_configured"))

    return ok(AnalysisResponse(call_id=call_id, status="pending"))


@router.post("/{call_id}/analysis/rerun")
async def rerun_call_analysis(
    call_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """Re-run post-call analysis for a completed call."""
    from turncall.api.v1.schemas.calls import AnalysisResponse

    call = await call_repo.get_call_by_id(session, call_id, project_id=auth.project_id)
    if call is None:
        raise NotFoundError("Call", str(call_id))

    if call.status not in ("completed", "failed"):
        from turncall.api.errors import BadRequestError

        raise BadRequestError("Can only analyze completed or failed calls")

    if not call.active_agent_id:
        from turncall.api.errors import BadRequestError

        raise BadRequestError("Call has no associated agent")

    from turncall.storage.repositories import agent_repo

    agent = await agent_repo.get_agent_by_id(session, call.active_agent_id)
    if agent is None:
        raise NotFoundError("Agent", str(call.active_agent_id))

    from turncall.services.call_analysis_trigger import trigger_post_call_analysis
    from turncall.storage.database import get_session_factory

    trigger_post_call_analysis(
        get_session_factory(),
        call_id,
        call.project_id,
        agent.config_blob,
    )

    return ok(AnalysisResponse(call_id=call_id, status="pending"))
