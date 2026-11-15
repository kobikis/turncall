"""Choosing a voice call's agent, accepted, before the call is opened (#487).

Two ways a call gets its agent, written once for every edge: the one a
call-init server answers (`call_init_agent`), and a stored agent of the
project (`stored_agent`) — through agent acceptance either way (#474), so
an agent that cannot run is refused before the row and before the caller is
answered, never at build. A number's call chooses between them by its routing
(`NumberSource`); a WebRTC request by what it names. Either answers a
`ChosenAgent`, or a `CallRefusal` the edge turns into its own reply.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.services.agent_acceptance import AcceptedAgent, AgentRefusal
from turncall.services.call_start import CallMetadata, CallParties, EdgeHandles
from turncall.storage.models import PhoneNumberRow

if TYPE_CHECKING:
    from turncall.services.call_init_resolver import CallInitResult
from turncall.storage.repositories import agent_repo


class RefusalReason(StrEnum):
    LIVE_CALL_LIMIT = "live_call_limit"
    NO_AGENT = "no_agent"
    AGENT_REFUSED = "agent_refused"


@dataclass(frozen=True)
class CallRefusal:
    """The call is not opened, and nothing of it is left: no row, no events,
    no slot. `busy` (over the live-call limit, `live_call_limit` its size):
    a carrier must refuse it unanswered, since answering only to say no bills
    minutes. Otherwise no agent can take it, and `message` is what a caller
    may be told; `refusal` is acceptance's answer, when it refused one."""

    reason: RefusalReason
    message: str
    refusal: AgentRefusal | None = None
    live_call_limit: int | None = None

    @property
    def busy(self) -> bool:
        return self.reason is RefusalReason.LIVE_CALL_LIMIT

    @classmethod
    def at_live_call_limit(cls, limit: int) -> CallRefusal:
        return cls(
            RefusalReason.LIVE_CALL_LIMIT,
            "The project is at its live-call limit.",
            live_call_limit=limit,
        )


# What call-init is told a number's call is (`call.type`).
INBOUND_PHONE_CALL = "inboundPhoneCall"
INBOUND_WHATSAPP_CALL = "inboundWhatsAppCall"

_UNRESOLVED = "Unable to resolve agent."
_UNAVAILABLE = "This agent is not available."


@dataclass(frozen=True)
class ChosenAgent:
    """The agent a source chose, accepted. `name` is its row's; None for an
    inline agent, which has none."""

    agent: AcceptedAgent
    name: str | None
    metadata: CallMetadata = field(default_factory=CallMetadata)


@dataclass(frozen=True, kw_only=True)
class NumberSource:
    """A call to one of the project's numbers: its agent is the one the
    number routes to — call-init's answer, or the bound agent. `weighted`:
    whether the number's A/B weights pick it by the caller (a phone call's;
    never WhatsApp's). `call_type` is what call-init is told the call is."""

    number: PhoneNumberRow
    parties: CallParties
    handles: EdgeHandles
    call_type: str
    weighted: bool

    @property
    def refused_unanswered(self) -> bool:
        """A number's call is refused at its carrier, before it is answered:
        `call.rejected` is how anyone hears of it."""
        return True

    async def choose_agent(
        self, session: AsyncSession, *, call_id: UUID
    ) -> ChosenAgent | CallRefusal:
        number = self.number
        if number.routing_target_type == "webhook" and number.server_url:
            return await call_init_agent(
                session,
                server_url=number.server_url,
                secret=number.server_url_secret,
                call_type=self.call_type,
                parties=self.parties,
                handles=self.handles,
                variables={},
                project_id=number.project_id,
                call_id=call_id,
            )
        if number.routing_target_type == "agent" and number.routing_target_id:
            return await stored_agent(
                session,
                self._routed_agent_id(),
                project_id=number.project_id,
                call_id=call_id,
            )
        logger.warning(
            "inbound_no_agent",
            routing_target_type=number.routing_target_type,
            routing_target_id=str(number.routing_target_id),
        )
        return CallRefusal(RefusalReason.NO_AGENT, _UNAVAILABLE)

    def _routed_agent_id(self) -> UUID:
        number = self.number
        if not (self.weighted and number.routing_weights):
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


async def call_init_agent(
    session: AsyncSession,
    *,
    server_url: str,
    secret: str | None,
    call_type: str,
    parties: CallParties,
    handles: EdgeHandles,
    variables: Mapping[str, str],
    project_id: UUID,
    call_id: UUID,
) -> ChosenAgent | CallRefusal:
    """The agent `server_url`'s call-init answers, accepted (#477), with what
    it adds to the call row: its variables, metadata, knowledge context and
    inline agent (the raw config, stored as `dynamic_config`). `variables`
    are the call's own, shown to call-init as `call.variables`. Only an agent
    of `project_id` is one (#475)."""
    from turncall.events.server_events import send_call_init
    from turncall.services.call_init_resolver import (
        accept_call_init_agent,
        resolve_call_init,
    )

    response = await send_call_init(
        server_url,
        call_id=str(call_id),
        from_number=parties.from_number,
        to_number=parties.to_number,
        provider_call_id=handles.provider_call_id,
        transport_type=call_type,
        secret=secret,
        variables=dict(variables) or None,
    )
    unresolved = CallRefusal(RefusalReason.NO_AGENT, _UNRESOLVED)
    if not (response.success and response.data):
        logger.warning("inbound_server_url_no_agent", server_url=server_url)
        return unresolved
    result = await resolve_call_init(session, response.data, project_id=project_id)
    outcome = await accept_call_init_agent(
        session, result, project_id=project_id, call_id=str(call_id)
    )
    if isinstance(outcome, AgentRefusal):
        return CallRefusal(RefusalReason.AGENT_REFUSED, _UNRESOLVED, outcome)
    if outcome is None:
        logger.warning("inbound_server_url_no_agent", server_url=server_url)
        return unresolved
    return _from_call_init(result, outcome)


def _from_call_init(result: CallInitResult, agent: AcceptedAgent) -> ChosenAgent:
    """Call-init's accepted agent, with what its answer adds to the row."""
    return ChosenAgent(
        agent,
        result.agent.name if result.agent else None,
        CallMetadata(
            result.dynamic_config,
            result.template_variables,
            result.metadata,
            result.knowledge_context,
        ),
    )


async def stored_agent(
    session: AsyncSession, agent_id: UUID, *, project_id: UUID, call_id: UUID
) -> ChosenAgent | CallRefusal:
    """The project's agent `agent_id`, when agent acceptance takes its config
    (`stored_call`, #480): one saved before a newer rule existed is refused
    here, with the reason logged, rather than failing in the build after the
    call was answered. Another project's is no agent."""
    from turncall.services.agent_acceptance import (
        AgentOrigin,
        accept_agent_row,
        log_refusal,
    )

    row = await agent_repo.get_agent_by_id(session, agent_id, project_id=project_id)
    if row is None:
        logger.warning("inbound_no_agent", routing_target_id=str(agent_id))
        return CallRefusal(RefusalReason.NO_AGENT, _UNAVAILABLE)
    # As a stored agent, so its linked knowledge bases count too (#484); the
    # row read above, not read again.
    outcome = await accept_agent_row(
        session,
        row,
        agent_id=row.id,
        project_id=project_id,
        origin=AgentOrigin.STORED_CALL,
    )
    if isinstance(outcome, AgentRefusal):
        log_refusal(
            outcome,
            origin=AgentOrigin.STORED_CALL,
            project_id=project_id,
            call_id=str(call_id),
            agent_id=str(agent_id),
        )
        return CallRefusal(RefusalReason.AGENT_REFUSED, _UNAVAILABLE, outcome)
    return ChosenAgent(outcome, row.name)
