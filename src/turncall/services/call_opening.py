"""Opening a voice call: from what an edge resolved to an opened call (#487).

Every voice edge verifies its own request, reads its own protocol and answers
in its own format (ADR-0022). Between the two it needs the same thing: the
agent the call runs (call-init's answer, A/B weights or the bound agent)
through agent acceptance (#474), a live-call slot (#224), the call row — an
inline agent's raw config as `dynamic_config` and no agent id (ADR-0017) —
and `call.initializing` + `call.started`. `open_call` does all of it once and
answers an `OpenedCall`, or a `CallRefusal` the edge turns into its reply:
TwiML, a Call Control command, an ARI `continue`, a WebRTC `429`, a WhatsApp
reject. `CallToRun.of` (`orchestrator/call_runner`) turns an opened call into
what `run_call` takes.

A slot is never left held without a row: any failure between taking it and
committing the row gives it back. From the commit on, the slot is the run's
to hold (`hold_live_call`).

This module is the edges' whole interface to opening: the agent choosers
(`call_agents`) and the row writers (`call_start`) are its parts, and what an
edge needs of them is imported from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import CallDirection, CallEventType
from turncall.services import telemetry_redaction
from turncall.services.agent_acceptance import AcceptedAgent
from turncall.services.call_agents import (
    INBOUND_PHONE_CALL,
    INBOUND_WHATSAPP_CALL,
    CallRefusal,
    ChosenAgent,
    NumberSource,
    RefusalReason,
)
from turncall.services.call_start import (
    CallMetadata,
    CallParties,
    CallStart,
    EdgeHandles,
    write_call_row,
    write_call_start,
)
from turncall.services.limits import (
    claim_live_call,
    limits_for_project,
    release_live_call,
)

__all__ = [
    "INBOUND_PHONE_CALL",
    "INBOUND_WHATSAPP_CALL",
    "CallMetadata",
    "CallParties",
    "CallRefusal",
    "CallSource",
    "CallStart",
    "ChosenAgent",
    "EdgeHandles",
    "NumberSource",
    "OpenedCall",
    "RefusalReason",
    "open_call",
]


@dataclass(frozen=True)
class OpenedCall:
    """A call whose row and start events are committed (`start`, what they
    were written from) and whose slot is taken under its id; `agent` the
    accepted agent it runs. Both name the same agent: None for an inline one."""

    start: CallStart
    agent: AcceptedAgent

    def __post_init__(self) -> None:
        if self.start.agent_id != self.agent.agent_id:
            raise ValueError("an opened call's row and agent name different agents")

    @property
    def call_id(self) -> UUID:
        return self.start.call_id

    @property
    def project_id(self) -> UUID:
        return self.start.project_id


class CallSource(Protocol):
    """What an edge resolved: the call's parties, its handles, whether a
    refusal is answered at a carrier (`refused_unanswered`, and so announced
    as `call.rejected`), and how its agent is chosen — once the call has its
    id, which call-init is told."""

    @property
    def parties(self) -> CallParties: ...

    @property
    def handles(self) -> EdgeHandles: ...

    @property
    def refused_unanswered(self) -> bool: ...

    async def choose_agent(
        self, session: AsyncSession, *, call_id: UUID
    ) -> ChosenAgent | CallRefusal: ...


async def open_call(
    source: CallSource, *, project_id: UUID, session: AsyncSession
) -> OpenedCall | CallRefusal:
    """Admit the call, choose and accept its agent, and commit its row with
    `call.initializing` + `call.started`; or why it is not opened, with
    nothing of it left behind. The slot is the run's to hold from here."""
    # From here on the call's logs follow its project's redaction (#314),
    # the edge's own included: this runs in its task.
    await telemetry_redaction.for_project(project_id, session=session)
    call_id = uuid4()
    # Before the agent: a call refused busy asks nobody for one. The slot is
    # taken under the call's id before the row exists (#224).
    busy = await _admit(session, source, project_id=project_id, call_id=call_id)
    if busy is not None:
        return busy
    try:
        opened = await _write(session, source, project_id=project_id, call_id=call_id)
    except BaseException:
        # No row, so no run will ever hold the slot or free it: an inline
        # agent holding a secret with no key to seal it (#338) fails here,
        # and every such call would otherwise keep a slot until it lapsed.
        await release_live_call(project_id, call_id)
        raise
    if isinstance(opened, CallRefusal):
        await release_live_call(project_id, call_id)
        return opened
    logger.info(
        "call_opened",
        transport=source.handles.transport_name,
        call_id=str(call_id),
        agent_id=str(opened.agent.agent_id or "dynamic"),
        provider_call_sid=source.handles.provider_call_sid,
    )
    return opened


async def _write(
    session: AsyncSession, source: CallSource, *, project_id: UUID, call_id: UUID
) -> OpenedCall | CallRefusal:
    """The source's agent chosen and the call's row and start events
    committed — before the edge answers: the media stream a carrier opens
    next reads the row in a session of its own."""
    chosen = await source.choose_agent(session, call_id=call_id)
    if isinstance(chosen, CallRefusal):
        return chosen
    start = CallStart(
        call_id=call_id,
        project_id=project_id,
        agent_id=chosen.agent.agent_id,
        agent_name=chosen.name,
        metadata=chosen.metadata,
        parties=source.parties,
        handles=source.handles,
        direction=CallDirection.INBOUND,
    )
    await write_call_row(session, start)
    await write_call_start(session, start)
    await session.commit()
    return OpenedCall(start=start, agent=chosen.agent)


async def _admit(
    session: AsyncSession, source: CallSource, *, project_id: UUID, call_id: UUID
) -> CallRefusal | None:
    """The call's live-call slot taken (None), or the refusal over the
    project's limit — announced as `call.rejected` when the source's refusal
    is answered at a carrier."""
    limits = await limits_for_project(session, project_id)
    if await claim_live_call(project_id, call_id, limits.live_calls):
        return None
    if source.refused_unanswered:
        await _announce_rejected(
            session, source, project_id=project_id, limit=limits.live_calls
        )
    return CallRefusal.at_live_call_limit(limits.live_calls)


async def _announce_rejected(
    session: AsyncSession, source: CallSource, *, project_id: UUID, limit: int
) -> None:
    """`call.rejected` for a call refused unanswered over the limit. There
    is no call row — the call never started — so it goes to webhook
    subscribers with no `call_id`, and the carrier's id in its payload."""
    from turncall.events.emit import dispatch_project_event

    handles, parties = source.handles, source.parties
    payload = {
        "reason": RefusalReason.LIVE_CALL_LIMIT.value,
        "limit": limit,
        "transport": handles.transport_name,
        "from_number": parties.from_number,
        "to_number": parties.to_number,
        "provider_call_id": handles.provider_call_id,
    }
    logger.warning("inbound_call_rejected", project_id=str(project_id), **payload)
    try:
        await dispatch_project_event(
            session,
            project_id=project_id,
            event_type=CallEventType.CALL_REJECTED.value,
            payload=payload,
        )
    except Exception:
        logger.exception("call_rejected_dispatch_error", project_id=str(project_id))
