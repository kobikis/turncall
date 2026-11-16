"""The shared media entry point (ADR-0022).

Each carrier owns its inbound edge — its webhook routes, its signature check,
its answer format, and the handshake its media stream opens with
(`twilio_media_stream.py` for Twilio). Every one of them ends by handing the
open stream to `run_media_stream`, which accepts the call's agent again from
the call record and runs the call through `call_runner.run_call` (#492) —
the run every voice edge gets. The serializer is chosen here from the call's
own carrier, in `create_carrier_transport`; past it, nothing learns which
carrier the call is on.

Before any of that, the stream has to prove it is the call's (a call id is
not a secret, and the socket gets the call's agent): it must carry the call's
media token (`services/media_token.py`), arrive on the route of the call's own
carrier, and be the call's first stream. Anything else is `MediaStreamRefused`
with nothing built and the call's live-call slot untouched — the slot is the
real stream's.
"""

from dataclasses import replace
from functools import partial
from typing import Any
from uuid import UUID

from fastapi import WebSocket
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.config import get_settings
from turncall.domain.enums import CallDirection
from turncall.orchestrator.call_runner import (
    CallToRun,
    TransportFactory,
    call_context,
    run_call,
)
from turncall.orchestrator.call_session import fail_unstarted_call
from turncall.orchestrator.transport_factory import (
    CARRIER_MEDIA,
    CarrierStream,
    create_carrier_transport,
)
from turncall.services.agent_acceptance import (
    AcceptedAgent,
    AgentOrigin,
    AgentRefusal,
    accept_agent,
    log_refusal,
)
from turncall.services.call_opening import (
    CallMetadata,
    CallParties,
    CallStart,
    EdgeHandles,
    OpenedCall,
)
from turncall.services.limits import release_live_call
from turncall.services.media_token import media_token_valid
from turncall.storage.database import create_session_factory, get_engine
from turncall.storage.models import CallRow


class MediaStreamRefused(Exception):
    """A stream that is not the call's own: nothing was built for it, and the
    edge closes it without touching the call (it belongs to the real one)."""


async def _claim(session_factory: Any, call_id: UUID, carrier: str, token: str) -> None:
    """Refuse a stream that is not this call's first, on its own carrier, with
    its token; claim the call's one stream for this one otherwise."""
    from turncall.storage.repositories import call_repo

    if not media_token_valid(call_id, token):
        logger.warning(
            "media_stream_refused: bad media token",
            call_id=str(call_id),
            carrier=carrier,
        )
        raise MediaStreamRefused("no valid media token")
    async with session_factory() as session:
        attached = await call_repo.attach_media(session, call_id, carrier=carrier)
        await session.commit()
        if attached:
            return
        row = await call_repo.get_call_by_id(session, call_id)
    reason = (
        "no such call"
        if row is None
        else f"the call is on {row.provider}"
        if row.provider != carrier
        else "the call already has a media stream"
        if row.media_attached_at is not None
        else f"the call is {row.status}"
    )
    logger.warning(
        "media_stream_refused: {reason}",
        reason=reason,
        call_id=str(call_id),
        carrier=carrier,
    )
    raise MediaStreamRefused(reason)


async def run_media_stream(
    websocket: WebSocket,
    call_id: UUID,
    stream: CarrierStream,
    *,
    carrier: str,
    token: str,
) -> None:
    """Run one call on an accepted, started carrier media stream.

    `carrier` is the edge's own — the route the stream arrived on — and
    `token` the media token its handshake carried. Raises `MediaStreamRefused`
    before anything is built for a stream that is not the call's (see the
    module docstring). The call runs through `run_call`, blocking: this
    returns when the call ends. Raises what the build raises; the carrier's
    edge owns the socket and decides what a failure means to it.
    """
    settings = get_settings()
    session_factory = create_session_factory(get_engine())
    await _claim(session_factory, call_id, carrier, token)
    call = await _opened_call(session_factory, call_id, carrier, stream)
    if call is None:
        return
    transport = TransportFactory(
        # The serializer comes from the call's carrier (ADR-0022) — which the
        # claim has made sure is also the edge's.
        make=partial(create_carrier_transport, carrier, websocket, stream),
        sample_rate=CARRIER_MEDIA[carrier].sample_rate,
    )
    await run_call(call, transport, own_task=False, settings=settings)


async def _opened_call(
    session_factory: async_sessionmaker[AsyncSession],
    call_id: UUID,
    carrier: str,
    stream: CarrierStream,
) -> CallToRun | None:
    """The call this stream claimed, as an opened call: its row's agent
    accepted again (#481) and rendered with the row's variables. None when it
    cannot run — the call failed or its slot freed where that is ours to do."""
    from turncall.storage.repositories import call_repo

    async with session_factory() as session:
        call_row = await call_repo.get_call_by_id(session, call_id)
        if call_row is None:
            logger.error("media_stream_call_not_found: {id}", id=str(call_id))
            return None
        origin = _runnable_origin(call_row, carrier)
        if origin is None:
            await release_live_call(call_row.project_id, call_id)
            return None
        outcome = await _accepted_agent(session, call_row, origin)
    start = _call_start(call_row, stream)
    if isinstance(outcome, AgentRefusal):
        await _refuse(start, outcome, origin, session_factory, stream)
        return None
    call = CallToRun.of(
        OpenedCall(start=start, agent=outcome), session_factory=session_factory
    )
    return replace(call, stream_sid=stream.stream_id)


def _runnable_origin(call_row: CallRow, carrier: str) -> AgentOrigin | None:
    """Where the row's agent came from, or None (logged) for a call that
    cannot run here at all."""
    if carrier not in CARRIER_MEDIA:
        # A call reaching a media stream this deployment has no serializer
        # for: decoding it as some other carrier's would be noise.
        logger.error(
            "media_stream_unknown_carrier", call_id=str(call_row.id), carrier=carrier
        )
        return None
    origin = _agent_origin(call_row)
    if origin is None:
        logger.error(
            "media_stream_no_config",
            agent_id=str(call_row.active_agent_id or "dynamic"),
        )
    return origin


def _call_start(call_row: CallRow, stream: CarrierStream) -> CallStart:
    """What the call was opened with, read back from its row. Its name was
    for `call.started`, long sent; a stream that does not name the call's
    REST handle (Asterisk's) leaves it to the row, which always has it."""
    metadata, own = CallMetadata.split(call_row.metadata_json or {})
    return CallStart(
        call_id=call_row.id,
        project_id=call_row.project_id,
        agent_id=call_row.active_agent_id,
        agent_name=None,
        metadata=metadata,
        parties=CallParties(
            from_number=call_row.from_number or "",
            to_number=call_row.to_number or "",
        ),
        handles=EdgeHandles(
            carrier=call_row.provider,
            provider_call_sid=stream.call_sid or call_row.provider_call_sid,
            row_metadata=own,
        ),
        direction=CallDirection(call_row.direction),
    )


def _agent_origin(call_row: CallRow) -> AgentOrigin | None:
    """Where the call record's agent came from: an inline one is still the
    call-init answer's raw config (ADR-0017), a stored one the number's or
    the dial's agent row. None when the record names no agent."""
    if (call_row.metadata_json or {}).get("dynamic_config"):
        return AgentOrigin.CALL_INIT
    if call_row.active_agent_id:
        return AgentOrigin.STORED_CALL
    return None


async def _accepted_agent(
    session: AsyncSession, call_row: CallRow, origin: AgentOrigin
) -> AcceptedAgent | AgentRefusal:
    """The call's agent through agent acceptance (#481), as `origin` reads it.

    The call record's agent was accepted when the call was opened — call-init
    for an inline one, the number's stored agent, a dial's — and is accepted
    again here, since the build takes nothing else: an inline agent as
    `call_init`, a stored one by id within the call's project. Cheap, and
    what makes an outbound dial's own agent (never accepted at the dial) and
    any future opener held to the rules without remembering to be."""
    source = (
        call_row.metadata_json["dynamic_config"]
        if origin is AgentOrigin.CALL_INIT
        else call_row.active_agent_id
    )
    return await accept_agent(
        source, project_id=call_row.project_id, origin=origin, session=session
    )


async def _refuse(
    start: CallStart,
    refusal: AgentRefusal,
    origin: AgentOrigin,
    session_factory: async_sessionmaker[AsyncSession],
    stream: CarrierStream,
) -> None:
    """A call whose agent cannot run: failed with the reason, as a build that
    raised was (`error.raised`, `call.ended`), and its slot freed. The carrier
    edge then closes the stream, which ends the call."""
    log_refusal(
        refusal,
        origin=origin,
        project_id=start.project_id,
        call_id=str(start.call_id),
    )
    context = call_context(
        call_id=start.call_id,
        project_id=start.project_id,
        agent_id=start.agent_id,
        call_sid=start.handles.call_sid,
        stream_sid=stream.stream_id,
        session_factory=session_factory,
        mcp_manager=None,
    )
    await fail_unstarted_call(context, ValueError(refusal.describe()))
    await release_live_call(start.project_id, start.call_id)
