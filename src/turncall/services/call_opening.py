"""Opening a voice call: from what an edge resolved to an opened call (#487).

Every voice edge verifies its own request, reads its own protocol and answers
in its own format (ADR-0022). Between the two it needs the same thing: the
agent the call runs (call-init's answer, A/B weights or the bound agent)
through agent acceptance (#474), a live-call slot (#224), the call row — an
inline agent's raw config as `dynamic_config` and no agent id (ADR-0017) —
and `call.initializing` + `call.started`. `open_call` does all of it once and
answers an `OpenedCall`, or a `CallRefusal` the edge turns into its reply:
TwiML, a Call Control command, an ARI `continue`, a WebRTC `429`, a WhatsApp
reject. `CallToRun.of` turns an opened call into what `run_call` takes.

A slot is never left held without a row: any failure between taking it and
committing the row gives it back. From the commit on, the slot is the run's
to hold (`hold_live_call`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import CallDirection, CallEventType
from turncall.services import telemetry_redaction
from turncall.services.agent_acceptance import AcceptedAgent
from turncall.services.limits import admit_inbound_call, release_live_call
from turncall.storage.models import CallRow, PhoneNumberRow
from turncall.storage.repositories import agent_repo, call_repo


class RefusalReason(StrEnum):
    LIVE_CALL_LIMIT = "live_call_limit"
    NO_AGENT = "no_agent"
    AGENT_REFUSED = "agent_refused"


@dataclass(frozen=True)
class CallRefusal:
    """The call is not opened, and nothing of it is left: no row, no events,
    no slot. `busy` (over the live-call limit): the carrier must refuse it
    unanswered, since answering only to say no bills minutes. Otherwise no
    agent can take it, and `message` is what a caller may be told; `field`
    names what acceptance refused, when it did."""

    reason: RefusalReason
    message: str
    field: str | None = None

    @property
    def busy(self) -> bool:
        return self.reason is RefusalReason.LIVE_CALL_LIMIT


AT_LIVE_CALL_LIMIT = CallRefusal(
    RefusalReason.LIVE_CALL_LIMIT, "The project is at its live-call limit."
)
_UNRESOLVED = "Unable to resolve agent."
_UNAVAILABLE = "This agent is not available."


@dataclass(frozen=True)
class CallParties:
    from_number: str
    to_number: str


@dataclass(frozen=True)
class EdgeHandles:
    """What only the edge knows of its call, in one place: its carrier
    (`calls.provider`), the carrier's id for the call, and keys of its own on
    the row's metadata (Telnyx's Call Control application).

    A call that no carrier carries names its `transport` (`webrtc`,
    `whatsapp`), and the id that transport gives it (WhatsApp's call id) as
    `transport_call_id` — never as the carrier's sid, which a hang-up or a
    transfer would act on."""

    carrier: str
    provider_call_sid: str | None = None
    row_metadata: Mapping[str, Any] = field(default_factory=dict)
    transport: str | None = None
    transport_call_id: str | None = None

    @property
    def transport_name(self) -> str:
        """How the call arrived, as its events and `call.rejected` say."""
        return self.transport or self.carrier

    @property
    def provider_call_id(self) -> str:
        """The call's id outside TurnCall, as call-init and `call.rejected`
        are told it."""
        return self.provider_call_sid or self.transport_call_id or ""

    @property
    def call_sid(self) -> str:
        """What the call's pipeline names it: the carrier's sid, else its
        transport's (`webrtc`, `whatsapp:<id>`)."""
        if self.provider_call_sid:
            return self.provider_call_sid
        if self.transport_call_id:
            return f"{self.transport_name}:{self.transport_call_id}"
        return self.transport_name


@dataclass(frozen=True)
class CallMetadata:
    """What the call row's metadata carries besides the edge's own keys."""

    dynamic_config: Mapping[str, Any] | None = None
    template_variables: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    knowledge_context: str | None = None

    def row_json(self) -> dict[str, Any]:
        """Each key only when it has a value, as every reader expects."""
        values: dict[str, Any] = {
            "dynamic_config": self.dynamic_config,
            "template_variables": self.template_variables,
            "metadata": self.metadata,
            "knowledge_context": self.knowledge_context,
        }
        return {
            key: dict(value) if isinstance(value, Mapping) else value
            for key, value in values.items()
            if value
        }

    @classmethod
    def split(cls, row: Mapping[str, Any]) -> tuple[CallMetadata, dict[str, Any]]:
        """A row's metadata read back: these keys, and the rest of it. The
        two written together (`write_call_row`) are `row` again."""
        known = {
            key: value
            for key, value in row.items()
            if key in _METADATA_KINDS
            and value
            and isinstance(value, _METADATA_KINDS[key])
        }
        rest = {key: value for key, value in row.items() if key not in known}
        return cls(**known), rest


_METADATA_KINDS: dict[str, type] = {
    "dynamic_config": Mapping,
    "template_variables": Mapping,
    "metadata": Mapping,
    "knowledge_context": str,
}


@dataclass(frozen=True)
class ChosenAgent:
    """The agent a source chose, accepted. `name` is its row's; None for an
    inline agent, which has none."""

    agent: AcceptedAgent
    name: str | None
    metadata: CallMetadata = CallMetadata()


@dataclass(frozen=True)
class OpenedCall:
    """A call whose row and start events are committed and whose slot is
    taken under `call_id`. `agent.agent_id` is None for an inline agent."""

    call_id: UUID
    project_id: UUID
    agent: AcceptedAgent
    agent_name: str | None
    metadata: CallMetadata
    parties: CallParties
    handles: EdgeHandles
    direction: CallDirection = CallDirection.INBOUND

    @property
    def start(self) -> CallStart:
        return CallStart(
            call_id=self.call_id,
            project_id=self.project_id,
            agent_id=self.agent.agent_id,
            agent_name=self.agent_name,
            metadata=self.metadata,
            parties=self.parties,
            handles=self.handles,
            direction=self.direction,
        )


@dataclass(frozen=True)
class CallStart:
    """What a call's row and start events are written from: an opened
    call's, or an outbound dial's, whose agent is accepted only once its
    media stream connects (#481). `agent_id` None for an inline agent."""

    call_id: UUID
    project_id: UUID
    agent_id: UUID | None
    agent_name: str | None
    metadata: CallMetadata
    parties: CallParties
    handles: EdgeHandles
    direction: CallDirection


class CallSource(Protocol):
    """What an edge resolved: the call's parties, its handles, and how its
    agent is chosen — once the call has its id, which call-init is told."""

    @property
    def parties(self) -> CallParties: ...

    @property
    def handles(self) -> EdgeHandles: ...

    async def choose_agent(
        self, session: AsyncSession, *, project_id: UUID, call_id: UUID
    ) -> ChosenAgent | CallRefusal: ...


@dataclass(frozen=True)
class NumberSource:
    """A call to one of the project's numbers: its agent is the one the
    number routes to — call-init's answer, or the bound agent (A/B weights
    picking by the caller)."""

    number: PhoneNumberRow
    parties: CallParties
    handles: EdgeHandles
    call_init_type: str = "inboundPhoneCall"

    async def choose_agent(
        self, session: AsyncSession, *, project_id: UUID, call_id: UUID
    ) -> ChosenAgent | CallRefusal:
        number = self.number
        if number.routing_target_type == "webhook" and number.server_url:
            return await _call_init_agent(session, self, call_id=call_id)
        if number.routing_target_type == "agent" and number.routing_target_id:
            return await _stored_agent(
                session, number, self._routed_agent_id(), call_id=call_id
            )
        logger.warning(
            "inbound_no_agent",
            routing_target_type=number.routing_target_type,
            routing_target_id=str(number.routing_target_id),
        )
        return CallRefusal(RefusalReason.NO_AGENT, _UNAVAILABLE)

    def _routed_agent_id(self) -> UUID:
        number = self.number
        if not number.routing_weights:
            return number.routing_target_id
        from turncall.services.weighted_routing import pick_agent_by_weight

        agent_id = pick_agent_by_weight(
            number.routing_weights, self.parties.from_number
        )
        logger.info(
            "ab_routing_selected",
            phone_number=self.parties.to_number,
            caller=self.parties.from_number,
            selected_agent_id=str(agent_id),
        )
        return agent_id


async def _call_init_agent(
    session: AsyncSession, source: NumberSource, *, call_id: UUID
) -> ChosenAgent | CallRefusal:
    """The agent call-init answers for the number, accepted (#477) before
    the row and before the caller is answered: one that cannot run is
    refused here, never at build."""
    from turncall.events.server_events import send_call_init
    from turncall.services.agent_acceptance import AgentRefusal
    from turncall.services.call_init_resolver import (
        accept_call_init_agent,
        resolve_call_init,
    )

    number = source.number
    response = await send_call_init(
        number.server_url,
        call_id=str(call_id),
        from_number=source.parties.from_number,
        to_number=source.parties.to_number,
        provider_call_id=source.handles.provider_call_id,
        transport_type=source.call_init_type,
        secret=number.server_url_secret,
    )
    unresolved = CallRefusal(RefusalReason.NO_AGENT, _UNRESOLVED)
    if not (response.success and response.data):
        logger.warning("inbound_server_url_no_agent", server_url=number.server_url)
        return unresolved
    result = await resolve_call_init(
        session, response.data, project_id=number.project_id
    )
    outcome = await accept_call_init_agent(
        session, result, project_id=number.project_id, call_id=str(call_id)
    )
    if isinstance(outcome, AgentRefusal):
        return CallRefusal(RefusalReason.AGENT_REFUSED, _UNRESOLVED, outcome.field)
    if outcome is None:
        logger.warning("inbound_server_url_no_agent", server_url=number.server_url)
        return unresolved
    return ChosenAgent(
        outcome,
        result.agent.name if result.agent else None,
        CallMetadata(
            result.dynamic_config,
            result.template_variables,
            result.metadata,
            result.knowledge_context,
        ),
    )


async def _stored_agent(
    session: AsyncSession, number: PhoneNumberRow, agent_id: UUID, *, call_id: UUID
) -> ChosenAgent | CallRefusal:
    """The agent the number routes to, when agent acceptance takes its config
    (`stored_call`, #480): one saved before a newer rule existed is refused
    here, with the reason logged, rather than failing in the build after the
    call was answered."""
    from turncall.services.agent_acceptance import (
        AgentOrigin,
        AgentRefusal,
        accept_agent_row,
        log_refusal,
    )

    row = await agent_repo.get_agent_by_id(
        session, agent_id, project_id=number.project_id
    )
    if row is None:
        logger.warning("inbound_no_agent", routing_target_id=str(agent_id))
        return CallRefusal(RefusalReason.NO_AGENT, _UNAVAILABLE)
    # As a stored agent, so its linked knowledge bases count too (#484); the
    # row read above, not read again.
    outcome = await accept_agent_row(
        session,
        row,
        agent_id=row.id,
        project_id=number.project_id,
        origin=AgentOrigin.STORED_CALL,
    )
    if isinstance(outcome, AgentRefusal):
        log_refusal(
            outcome,
            origin=AgentOrigin.STORED_CALL,
            project_id=number.project_id,
            call_id=str(call_id),
            agent_id=str(agent_id),
        )
        return CallRefusal(RefusalReason.AGENT_REFUSED, _UNAVAILABLE, outcome.field)
    return ChosenAgent(outcome, row.name)


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
    # taken under the call's id before the row exists (#224);
    # `admit_inbound_call` has already announced `call.rejected` when it says no.
    if not await admit_inbound_call(
        session,
        project_id=project_id,
        call_id=call_id,
        transport=source.handles.transport_name,
        from_number=source.parties.from_number,
        to_number=source.parties.to_number,
        provider_call_id=source.handles.provider_call_id,
    ):
        return AT_LIVE_CALL_LIMIT
    try:
        chosen = await source.choose_agent(
            session, project_id=project_id, call_id=call_id
        )
        if isinstance(chosen, CallRefusal):
            await release_live_call(project_id, call_id)
            return chosen
        opened = OpenedCall(
            call_id=call_id,
            project_id=project_id,
            agent=chosen.agent,
            agent_name=chosen.name,
            metadata=chosen.metadata,
            parties=source.parties,
            handles=source.handles,
        )
        await write_call(session, opened)
        # Committed before the edge answers: the media stream the carrier
        # opens next reads the row in a session of its own.
        await session.commit()
    except BaseException:
        # No row, so no run will ever hold the slot or free it: an inline
        # agent holding a secret with no key to seal it (#338) fails here,
        # and every such call would otherwise keep a slot until it lapsed.
        await release_live_call(project_id, call_id)
        raise
    logger.info(
        "call_opened",
        transport=source.handles.transport_name,
        call_id=str(call_id),
        agent_id=str(opened.agent.agent_id or "dynamic"),
        provider_call_sid=source.handles.provider_call_sid,
    )
    return opened


async def write_call(session: AsyncSession, opened: OpenedCall) -> CallRow:
    """The opened call's row and its `call.initializing` + `call.started`,
    flushed, not committed — the one write every voice call starts with."""
    call = await write_call_row(session, opened.start)
    await write_call_start(session, opened.start)
    return call


async def write_call_row(session: AsyncSession, start: CallStart) -> CallRow:
    """The first half of `write_call`, for an outbound dial: its row is
    written before the carrier is asked (a row that cannot be sealed, #338,
    then dials nothing), and its start events once the carrier answered."""
    parties, handles = start.parties, start.handles
    return await call_repo.create_call(
        session,
        project_id=start.project_id,
        call_id=start.call_id,
        direction=start.direction,
        # A browser call has no numbers: stored as none, not as "".
        from_number=parties.from_number or None,
        to_number=parties.to_number or None,
        # None for an inline agent: it has no row (ADR-0017).
        active_agent_id=start.agent_id,
        provider_call_sid=handles.provider_call_sid,
        metadata_json={**start.metadata.row_json(), **handles.row_metadata},
        carrier=handles.carrier,
    )


async def write_call_start(session: AsyncSession, start: CallStart) -> None:
    """The second half: `call.initializing` + `call.started`, stored and
    sent. The carrier's id for the call is the one `start` carries."""
    from turncall.events.emit import emit_call_event

    parties, handles = start.parties, start.handles
    await emit_call_event(
        session,
        call_id=start.call_id,
        project_id=start.project_id,
        event_type=CallEventType.CALL_INITIALIZING,
        payload={
            "from_number": parties.from_number,
            "to_number": parties.to_number,
            "direction": start.direction.value,
            "transport": handles.transport_name,
        },
    )
    await emit_call_event(
        session,
        call_id=start.call_id,
        project_id=start.project_id,
        event_type=CallEventType.CALL_STARTED,
        payload={
            "provider_call_sid": handles.provider_call_sid,
            "from_number": parties.from_number,
            "to_number": parties.to_number,
            # Also in the envelope (ADR-0007); kept here, where an outbound
            # call's subscribers read it first. None for an inline agent.
            "agent_id": str(start.agent_id) if start.agent_id else None,
            "agent_name": start.agent_name or "dynamic",
            "dynamic_config": bool(start.metadata.dynamic_config),
            "direction": start.direction.value,
            "transport": handles.transport_name,
        },
    )
