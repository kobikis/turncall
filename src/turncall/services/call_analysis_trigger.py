"""Post-call processing: analysis + comprehensive call.ended webhook.

When a call completes, this module:
1. Gathers the transcript from DB (for `call.ended`; analysis reads the raw
   conversation instead, see `_analysis_transcript`)
2. Fetches recording URL(s) from recording.ready events
3. Runs LLM analysis (if enabled)
4. Stores analysis results on the call record, redacted if the project
   redacts (#313, ADR-0024: the call read serves them)
5. Dispatches a comprehensive call.ended webhook with all data

Where it runs (#313): analysis reads the call's raw conversation, which only
the process that ran the call holds (`services/live_transcript`), and only for
a few minutes. Post-call is started once per call (`start_post_call`, #356):
by the pipeline's own finalize, once its last lines are stored; by Twilio's
`/status` callback or the end API only for a call no pipeline ran, deferring
it to the pipeline otherwise (`start_or_defer_post_call`); and by the sweep
(`run_sweep`) for a deferred call whose pipeline died. The last two
can land on another replica. For a project that redacts, with analysis on,
such a replica hands the run to the process holding the conversation over
Redis pub/sub (`live_transcript.ask_holder`); no transcript crosses Redis,
only the request. When no process holds it (a restart, or the
minutes are up, as for a re-run), the run is local, on the stored and redacted
transcript, and logged as such.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.domain.enums import CallEventType, EndedReason
from turncall.domain.models import (
    AnalysisConfig,
    AWSConfig,
    LLMConfig,
    RetentionConfig,
)
from turncall.services import (
    live_transcript,
    redaction,
    retention,
    telemetry_redaction,
)
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
    output is redacted on the way out. `stored` when no process holds the
    conversation any more (a restart, a re-run minutes later) — redacted if
    the project redacts."""
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
# Post-call runs once the call's pipeline has finished (#356), or for a call no
# pipeline ran, so `none` — capture never started — can no longer change: a
# pipeline that failed to build, or one cancelled before it started (a caller
# who hung up while its services connected: pipecat 1.13 sends no frame to a
# processor not yet started, #367).
_TERMINAL_RECORDING = {"none", "completed", "failed", "expired"}


async def _wait_for_recording(
    session_factory: async_sessionmaker[AsyncSession],
    call_id: UUID,
) -> tuple[str | None, str]:
    """Poll recording_status until terminal (none/completed/failed) or timeout.

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
    *,
    here: bool = False,
) -> None:
    """Full post-call processing: transcript, recording, analysis, webhook.

    `here`: run in this process whoever holds the conversation; the holder's
    own run, asked for by another replica (`post_call_here`).
    """
    from turncall.storage.repositories import call_repo

    try:
        policy = await redaction.policy_for_project(
            project_id, sessions=session_factory
        )
        # This task's logs, the analysis LLM's included, follow the project.
        telemetry_redaction.use(policy)
        if (
            analysis_config.enabled
            and policy is not None
            and live_transcript.lines(call_id) is None
        ):
            if (
                not here
                and await live_transcript.ask_holder(call_id, live_transcript.POST_CALL)
                is not None
            ):
                logger.info("post_call_handed_to_call_holder", call_id=str(call_id))
                return
            # Nobody holds it: analysis reads the stored, redacted transcript,
            # and a takeaway asking for the caller's number gets the redaction.
            logger.warning(
                "post_call_analysis_from_stored_transcript",
                call_id=str(call_id),
                reason="no process holds the call's conversation",
            )

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

            # Store analysis on call record. Redacted first, for a project that
            # redacts: the call read serves it, and analysis read the raw
            # conversation (#313). The top-level keys are ours (`model`,
            # `analyzed_at` are skipped); everything under them is analysed
            # whatever its key, so `takeaways` and `structured_data`, whose
            # keys are the user's, are redacted in full (#314).
            # Before the session, as every redaction wait is (events/emit).
            if policy is not None:
                analysis_dict = await redaction.redact_payload(analysis_dict, policy)
            async with session_factory() as session:
                stored = await call_repo.update_call_analysis(
                    session, call_id, analysis_dict
                )
                await session.commit()
            if stored is None:
                # The janitor deleted the transcript while this ran (#316): the
                # verdict is on data that is gone, and so is call.ended.
                logger.info("post_call_transcript_expired", call_id=str(call_id))
                return

        # 3. Wait for the recording to finish persisting (bounded — see
        # _wait_for_recording). Gates call.ended on the recording being done
        # and available, without blocking forever on a failed/slow upload.
        recording_url, recording_status = await _wait_for_recording(
            session_factory, call_id
        )

        # 4. Load the call and its derived fields (ended_reason, ADR-0008) —
        # the same function the call read uses, so the two cannot disagree.
        # And the retention that applies to it (#316): call.ended states what
        # will be kept, and for how long.
        async with session_factory() as session:
            call = await call_repo.get_call_by_id(session, call_id)
            derived = (await call_view_fields(session, [call]))[call.id] if call else {}
            kept = await retention.for_call(session, call) if call else None
        if call is not None and getattr(call, "transcript_expired_at", None):
            # A re-run on a call whose transcript expired since it was asked
            # for (#316): there is nothing left to report on.
            logger.info("post_call_transcript_expired", call_id=str(call_id))
            return

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
        call_ended_payload["retention"] = (kept or RetentionConfig()).as_payload()
        call_ended_payload["recording_status"] = recording_status
        if recording_url:
            call_ended_payload["recording_url"] = recording_url
        if analysis_dict:
            call_ended_payload["summary"] = analysis_dict.get("summary")
            call_ended_payload["analysis"] = analysis_dict

        # 6. Dispatch comprehensive call.ended webhook. Redacted before the
        # session (events/emit). What was read back from this project's
        # stored events (the transcript, failovers) and the analysis stored
        # just above are redacted already, and pass through: only the rest
        # (the call's metadata) is analysed.
        from turncall.events.emit import dispatch_call_event, redact

        ended = await redact(
            project_id,
            CallEventType.CALL_ENDED,
            call_ended_payload,
            stored=("transcript", "failovers", "summary", "analysis"),
            sessions=session_factory,
        )
        async with session_factory() as session:
            await dispatch_call_event(
                session,
                call_id=call_id,
                project_id=project_id,
                event_type=CallEventType.CALL_ENDED,
                payload=ended,
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


async def start_post_call(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    call: Any,
    *,
    deferred: bool = False,
) -> bool:
    """Start the call's post-call processing, which sends `call.ended`, unless
    another path already has (`call_repo.claim_post_call`); whether this did.
    `deferred`: only if an ending left it to the call's pipeline. Commits
    `session` either way, before starting it: the run reads the call in a
    session of its own (#328).
    """
    from turncall.storage.repositories import call_repo

    claimed = await call_repo.claim_post_call(session, call.id, deferred=deferred)
    await session.commit()
    if not claimed:
        return False
    try:
        # Not gated on active_agent_id: a call running an inline agent has
        # none, and this is what dispatches call.ended.
        config = await config_for_call(session, call)
        if config is not None:
            trigger_post_call_analysis(
                session_factory, call.id, call.project_id, config
            )
    except Exception:
        logger.exception("analysis_trigger_error", call_id=str(call.id))
    return True


async def start_or_defer_post_call(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    call: Any,
) -> None:
    """Post-call processing for a call the carrier or the end API just ended
    (`call`: the row as ended, in `session`'s transaction, not yet
    committed). A call no pipeline ran gets it now. One a pipeline runs (its
    media stream attached) is left to the pipeline, which starts it once the
    call's last lines are stored: started here, the transcript `call.ended`
    carries could lack them (#356). Commits `session`.
    """
    from turncall.storage.repositories import call_repo

    if call.media_attached_at is not None:
        # Committed with the end: a pipeline finishing meanwhile waits on the
        # row, then finds the call ended and deferred to it, never just ended.
        await call_repo.defer_post_call(session, call.id)
        await session.commit()
        return
    await session.commit()  # the end, whatever post-call processing then does
    await start_post_call(session_factory, session, call)


# The sweep (#356): how long a pipeline gets, after its call was deferred to
# it, before the sweep starts post-call processing itself, and how often a
# replica looks. A pipeline flushes its last lines within seconds of the call
# ending; one still running past the grace is left alone.
SWEEP_GRACE_SECONDS = 120
SWEEP_INTERVAL_SECONDS = 30


async def _still_running(call_id: UUID) -> bool:
    """Whether the call's pipeline still runs, on any replica. Without Redis
    nothing can say, and a call ended minutes ago is taken for over."""
    from turncall.services import live_call_channel
    from turncall.storage.redis import get_redis

    try:
        redis = get_redis()
    except RuntimeError:
        return False
    return await live_call_channel.is_running(redis, call_id)


async def sweep_deferred(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> int:
    """Start post-call processing for each call deferred to its pipeline more
    than the grace ago whose pipeline is no longer running and never started
    it — its process died. How many were started here."""
    from turncall.storage.repositories import call_repo

    async with session_factory() as session:
        calls = await call_repo.list_post_call_deferred(
            session, deferred_before=now - timedelta(seconds=SWEEP_GRACE_SECONDS)
        )
    started = 0
    for call in calls:
        try:
            if await _still_running(call.id):
                continue
            async with session_factory() as session:
                if await start_post_call(session_factory, session, call, deferred=True):
                    started += 1
                    logger.warning("post_call_swept", call_id=str(call.id))
        except Exception:
            logger.exception("post_call_sweep_call_error", call_id=str(call.id))
    return started


async def run_sweep(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval: float = SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep every `interval` seconds until cancelled (the API's lifespan).
    Every replica sweeps; the claim lets one start each call."""
    while True:
        try:
            await asyncio.sleep(interval)
            await sweep_deferred(session_factory, now=datetime.now(UTC))
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("post_call_sweep_error")


def trigger_post_call_analysis(
    session_factory: async_sessionmaker[AsyncSession],
    call_id: UUID,
    project_id: UUID,
    agent_config_blob: dict[str, Any],
    *,
    here: bool = False,
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
            here=here,
        ),
        name=f"post-call-{call_id}",
    )
    _POST_CALL_TASKS.add(task)
    task.add_done_callback(_POST_CALL_TASKS.discard)
    return task


async def post_call_here(body: dict[str, Any]) -> dict[str, Any]:
    """Another replica's request to this one, which holds the call's
    conversation: run post-call processing here (`live_transcript.POST_CALL`).
    Answers once it is started, not finished."""
    from turncall.services.live_call_channel import ControlRefused
    from turncall.storage.database import get_session_factory
    from turncall.storage.repositories import call_repo

    call_id = UUID(str(body["call_id"]))
    factory = get_session_factory()
    async with factory() as session:
        call = await call_repo.get_call_by_id(session, call_id)
        config = await config_for_call(session, call) if call else None
    if call is None or config is None:
        raise ControlRefused("No call or agent config to analyse")
    trigger_post_call_analysis(factory, call_id, call.project_id, config, here=True)
    return {}
