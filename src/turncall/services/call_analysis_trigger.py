"""Post-call processing: analysis + comprehensive call.ended webhook.

When a call completes, this module:
1. Gathers the transcript from DB (for `call.ended`; analysis reads the raw
   conversation instead, see `_analysis_transcript`)
2. Fetches recording URL(s) from recording.ready events
3. Runs LLM analysis (if enabled)
4. Stores analysis results on the call record
5. Dispatches a comprehensive call.ended webhook with all data
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.domain.enums import CallEventType, EndedReason
from turncall.domain.models import AnalysisConfig, AWSConfig, LLMConfig
from turncall.services.call_view import call_view_fields, public_metadata

if TYPE_CHECKING:
    from turncall.services.credentials import Credentials


async def _gather_transcript(
    session_factory: async_sessionmaker[AsyncSession],
    call_id: UUID,
) -> list[dict[str, Any]]:
    """Load transcript events and return structured entries."""
    from turncall.storage.repositories import call_repo

    async with session_factory() as session:
        events = await call_repo.list_call_events(
            session,
            call_id,
            event_type=CallEventType.TRANSCRIPT_FINAL,
            limit=1000,
        )
        return [
            {
                "role": (
                    # "role" is the current key; "user_id" is the legacy key for
                    # events written before the rename (calls in-flight at deploy).
                    "agent"
                    if (e.payload.get("role") or e.payload.get("user_id"))
                    == "assistant"
                    else "customer"
                ),
                "text": e.payload.get("text", ""),
                "timestamp": e.internal_timestamp.isoformat(),
            }
            for e in events
            if e.payload.get("text", "").strip()
        ]


def _analysis_transcript(
    call_id: UUID, stored: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """What analysis and takeaways read: the conversation as said (#313,
    ADR-0024). Redaction rewrites the stored transcript; extracting the
    caller's callback number from `<PHONE_NUMBER>` is no extraction, so they
    read this process's raw copy (services/live_transcript) and only their
    output is redacted on the way out. `stored` when this process does not
    hold the conversation (another replica finalized the call, a restart) —
    redacted if the project redacts."""
    from turncall.services import live_transcript

    held = live_transcript.lines(call_id)
    if held is not None:
        return [
            {
                "text": line.text,
                "user_id": "assistant" if line.is_agent else "customer",
            }
            for line in held
            if line.text.strip()
        ]
    if stored:
        logger.info("post_call_analysis_from_stored_transcript", call_id=str(call_id))
    return [
        {
            "text": e["text"],
            "user_id": "assistant" if e["role"] == "agent" else "customer",
        }
        for e in stored
    ]


# call.ended must wait for the recording to finish persisting, but never block
# forever on a stuck/slow upload — it's the subscriber's mandatory end-of-call
# signal. Cap the wait and fire regardless of the recording outcome.
RECORDING_WAIT_TIMEOUT_S = 15.0
RECORDING_POLL_INTERVAL_S = 0.5
_TERMINAL_RECORDING = {"completed", "failed"}


async def _wait_for_recording(
    session_factory: async_sessionmaker[AsyncSession],
    call_id: UUID,
) -> tuple[str | None, str]:
    """Poll recording_status until terminal (completed/failed) or timeout.

    The recording is flushed by a separate task on WebSocket disconnect, so its
    status/url land asynchronously. Returns (recording_url, recording_status).
    """
    from turncall.storage.repositories import call_repo

    waited = 0.0
    url: str | None = None
    status = "none"
    while True:
        async with session_factory() as session:
            call = await call_repo.get_call_by_id(session, call_id)
            if call:
                status = call.recording_status
                url = call.recording_url
        if status in _TERMINAL_RECORDING:
            return url, status
        if call and call.started_at is None:
            # Its pipeline never ran (a build failure), so nothing is recording.
            return url, status
        if waited >= RECORDING_WAIT_TIMEOUT_S:
            logger.warning(
                "post_call_recording_wait_timeout",
                call_id=str(call_id),
                status=status,
            )
            return url, status
        await asyncio.sleep(RECORDING_POLL_INTERVAL_S)
        waited += RECORDING_POLL_INTERVAL_S


async def _project_credentials(
    session_factory: async_sessionmaker[AsyncSession], project_id: UUID
) -> Credentials | None:
    """The project's keys for the analysis LLM — the account its calls ran on.
    Unreadable falls back to the platform's (None) with a log: analysis is
    best-effort, and call.ended must still go out."""
    from turncall.services.credentials import for_project

    try:
        async with session_factory() as session:
            return await for_project(session, project_id)
    except Exception:
        logger.exception("post_call_credentials_unreadable", project_id=str(project_id))
        return None


async def _run_post_call(
    session_factory: async_sessionmaker[AsyncSession],
    call_id: UUID,
    project_id: UUID,
    analysis_config: AnalysisConfig,
    llm_config: LLMConfig,
    system_prompt: str,
    aws_config: AWSConfig | None = None,
) -> None:
    """Full post-call processing: transcript, recording, analysis, webhook."""
    from turncall.storage.repositories import call_repo

    try:
        # 1. Gather transcript
        transcript = await _gather_transcript(session_factory, call_id)
        logger.info(
            "post_call_transcript_loaded",
            call_id=str(call_id),
            entries=len(transcript),
        )

        # 2. Run LLM analysis first — it runs while the concurrent recording
        # flush finishes, so the wait in step 3 is usually already terminal.
        analysis_dict: dict[str, Any] | None = None
        if analysis_config.enabled:
            from turncall.services.call_analysis import analyze_call

            credentials = await _project_credentials(session_factory, project_id)

            transcript_payloads = _analysis_transcript(call_id, transcript)

            result = await analyze_call(
                transcript_payloads,
                analysis_config,
                llm_config,
                system_prompt=system_prompt,
                aws=aws_config,
                credentials=credentials,
            )
            analysis_dict = result.to_dict()

            # Takeaways (ADR-0013): reusable structured outputs, one concurrent
            # LLM extraction per attached takeaway, keyed by name.
            takeaways_dict = await _extract_takeaways(
                session_factory,
                project_id,
                analysis_config,
                llm_config,
                transcript_payloads,
                system_prompt,
                aws_config,
                credentials,
            )
            if takeaways_dict:
                analysis_dict["takeaways"] = takeaways_dict

            logger.info(
                "post_call_analysis_done",
                call_id=str(call_id),
                model=result.model,
                duration_ms=result.duration_ms,
                takeaways=len(takeaways_dict or {}),
            )

            # Store analysis on call record
            async with session_factory() as session:
                await call_repo.update_call_analysis(session, call_id, analysis_dict)
                await session.commit()

        # 3. Wait for the recording to finish persisting (bounded — see
        # _wait_for_recording). Gates call.ended on the recording being done
        # and available, without blocking forever on a failed/slow upload.
        recording_url, recording_status = await _wait_for_recording(
            session_factory, call_id
        )

        # 4. Load the call and its derived fields (ended_reason, ADR-0008) —
        # the same function the call read uses, so the two cannot disagree.
        async with session_factory() as session:
            call = await call_repo.get_call_by_id(session, call_id)
            derived = (await call_view_fields(session, [call]))[call.id] if call else {}

        # 5. Build comprehensive call.ended payload
        call_ended_payload: dict[str, Any] = {
            "transcript": transcript,
            **derived,
            # A call.ended always names a reason, as it did before the view.
            "ended_reason": derived.get("ended_reason") or EndedReason.UNKNOWN.value,
        }
        if call:
            call_ended_payload["status"] = call.status
            call_ended_payload["from_number"] = call.from_number
            call_ended_payload["to_number"] = call.to_number
            call_ended_payload["direction"] = call.direction
            call_ended_payload["duration_ms"] = call.duration_ms
            call_ended_payload["provider_call_sid"] = call.provider_call_sid
            call_ended_payload["metadata"] = public_metadata(call.metadata_json)
            call_ended_payload["started_at"] = (
                call.started_at.isoformat() if call.started_at else None
            )
            call_ended_payload["ended_at"] = (
                call.ended_at.isoformat() if call.ended_at else None
            )
        call_ended_payload["recording_status"] = recording_status
        if recording_url:
            call_ended_payload["recording_url"] = recording_url
        if analysis_dict:
            call_ended_payload["summary"] = analysis_dict.get("summary")
            call_ended_payload["analysis"] = analysis_dict

        # 6. Dispatch comprehensive call.ended webhook
        async with session_factory() as session:
            from turncall.events.emit import dispatch_call_event

            await dispatch_call_event(
                session,
                call_id=call_id,
                project_id=project_id,
                event_type=CallEventType.CALL_ENDED,
                payload=call_ended_payload,
            )

        logger.info(
            "post_call_completed",
            call_id=str(call_id),
            has_transcript=len(transcript) > 0,
            has_recording=recording_url is not None,
            has_analysis=analysis_dict is not None,
        )
    except Exception:
        logger.exception("post_call_error", call_id=str(call_id))


async def run_analysis_inline(
    session_factory: async_sessionmaker[AsyncSession],
    call_id: UUID,
    project_id: UUID,
    agent_config_blob: dict[str, Any],
) -> None:
    """Run post-call processing inline (awaited)."""
    analysis_raw = agent_config_blob.get("analysis", {})
    analysis_config = AnalysisConfig(**analysis_raw)
    llm_raw = agent_config_blob.get("llm", {})
    llm_config = LLMConfig(**llm_raw)
    aws_config = AWSConfig(**(agent_config_blob.get("aws") or {}))
    system_prompt = agent_config_blob.get("system_prompt", "")

    await _run_post_call(
        session_factory,
        call_id,
        project_id,
        analysis_config,
        llm_config,
        system_prompt,
        aws_config,
    )


async def _extract_takeaways(
    session_factory: async_sessionmaker[AsyncSession],
    project_id: UUID,
    analysis_config: AnalysisConfig,
    llm_config: LLMConfig,
    transcript_payloads: list[dict[str, Any]],
    system_prompt: str,
    aws_config: AWSConfig | None = None,
    credentials: Credentials | None = None,
) -> dict[str, Any] | None:
    """Load the agent's attached takeaways and extract them concurrently.
    Best-effort: failures produce valid=False entries, never an exception."""
    if not analysis_config.takeaway_ids:
        return None
    try:
        from turncall.services.call_analysis import (
            _build_transcript_text,
            extract_takeaway,
        )
        from turncall.storage.repositories import takeaway_repo

        ids = [UUID(t) for t in analysis_config.takeaway_ids]
        async with session_factory() as session:
            rows = await takeaway_repo.list_by_ids(session, project_id, ids)
        missing = len(ids) - len(rows)
        if missing:
            logger.warning(
                "takeaways_missing", project_id=str(project_id), count=missing
            )
        if not rows:
            return None

        transcript = _build_transcript_text(transcript_payloads)
        results = await asyncio.gather(
            *(
                extract_takeaway(
                    transcript,
                    name=r.name,
                    schema=r.schema,
                    llm_config=llm_config,
                    aws=aws_config,
                    credentials=credentials,
                    description=r.description,
                    prompt=r.prompt,
                    model=r.model or analysis_config.model,
                    system_prompt=system_prompt,
                )
                for r in rows
            )
        )
        return {row.name: res for row, res in zip(rows, results, strict=True)}
    except Exception:
        logger.exception("takeaway_extraction_error")
        return None


# Post-call tasks are fire-and-forget; callers discard the returned task and
# asyncio holds only a weak reference, so without this set the GC could collect
# a task mid-flight — losing the mandatory call.ended webhook + analysis.
_POST_CALL_TASKS: set[asyncio.Task] = set()


def pending_post_call_tasks() -> frozenset[asyncio.Task]:
    """Post-call processing still running — a shutdown drain waits for it,
    since it is what sends `call.ended`."""
    return frozenset(_POST_CALL_TASKS)


async def config_for_call(session: AsyncSession, call: Any) -> dict[str, Any] | None:
    """The agent config a call actually ran with.

    Usually the agent's, by id. A call whose agent arrived from call-init as an
    inline config has no agent row at all, and its config is the one stored on
    the call — without this it gets no post-call processing, and therefore no
    `call.ended` webhook, which is the event the whole post-call path hangs off.
    """
    if call.active_agent_id:
        from turncall.storage.repositories import agent_repo

        agent = await agent_repo.get_agent_by_id(session, call.active_agent_id)
        if agent is not None:
            return agent.config_blob
    dynamic = (call.metadata_json or {}).get("dynamic_config")
    return dynamic if isinstance(dynamic, dict) else None


def trigger_post_call_analysis(
    session_factory: async_sessionmaker[AsyncSession],
    call_id: UUID,
    project_id: UUID,
    agent_config_blob: dict[str, Any],
) -> asyncio.Task:
    """Fire-and-forget post-call processing.

    Always runs (gathers transcript + recording even if analysis disabled).
    """
    analysis_raw = agent_config_blob.get("analysis", {})
    analysis_config = AnalysisConfig(**analysis_raw)
    llm_raw = agent_config_blob.get("llm", {})
    llm_config = LLMConfig(**llm_raw)
    aws_config = AWSConfig(**(agent_config_blob.get("aws") or {}))
    system_prompt = agent_config_blob.get("system_prompt", "")

    task = asyncio.create_task(
        _run_post_call(
            session_factory,
            call_id,
            project_id,
            analysis_config,
            llm_config,
            system_prompt,
            aws_config,
        ),
        name=f"post-call-{call_id}",
    )
    _POST_CALL_TASKS.add(task)
    task.add_done_callback(_POST_CALL_TASKS.discard)
    return task
