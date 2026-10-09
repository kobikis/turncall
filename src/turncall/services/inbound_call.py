"""Admitting an inbound phone call, whichever carrier rang (ADR-0022).

Each carrier's edge reads its own wire format and answers in its own way, but
what happens between — the number's agent resolved (statically, by A/B weight
or by the call-init webhook), the call row written, `call.initializing` and
`call.started` emitted — is one thing, so it lives here once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from loguru import logger

from turncall.domain.enums import CallDirection, CallEventType
from turncall.storage.repositories import agent_repo, call_repo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from turncall.storage.models import CallRow, PhoneNumberRow


@dataclass(frozen=True)
class Refused:
    """The call cannot be taken; `message` is what the caller may be told."""

    message: str


@dataclass(frozen=True)
class _Resolved:
    agent: Any = None
    dynamic_config: dict[str, Any] | None = None
    template_variables: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    knowledge_context: str | None = None


async def _resolve_agent(
    session: AsyncSession,
    number: PhoneNumberRow,
    *,
    from_number: str,
    to_number: str,
    call_sid: str,
) -> _Resolved:
    if number.routing_target_type == "webhook" and number.server_url:
        from turncall.events.server_events import send_call_init
        from turncall.services.call_init_resolver import resolve_call_init

        response = await send_call_init(
            number.server_url,
            call_id=str(uuid4()),
            from_number=from_number,
            to_number=to_number,
            call_sid=call_sid,
            transport_type="inboundPhoneCall",
            secret=number.server_url_secret,
        )
        if not (response.success and response.data):
            return _Resolved()
        result = await resolve_call_init(session, response.data)
        return _Resolved(
            agent=result.agent,
            dynamic_config=result.dynamic_config,
            template_variables=result.template_variables,
            metadata=result.metadata,
            knowledge_context=result.knowledge_context,
        )

    if number.routing_target_type == "agent" and number.routing_target_id:
        agent_id = number.routing_target_id
        if number.routing_weights:
            from turncall.services.weighted_routing import pick_agent_by_weight

            agent_id = pick_agent_by_weight(number.routing_weights, from_number)
            logger.info(
                "ab_routing_selected",
                phone_number=to_number,
                caller=from_number,
                selected_agent_id=str(agent_id),
            )
        return _Resolved(agent=await agent_repo.get_agent_by_id(session, agent_id))
    return _Resolved()


async def admit_inbound_call(
    session: AsyncSession,
    number: PhoneNumberRow,
    *,
    from_number: str,
    to_number: str,
    call_sid: str,
    carrier: str,
) -> CallRow | Refused:
    """Resolve the number's agent and open the call record, or say why not."""
    found = await _resolve_agent(
        session, number, from_number=from_number, to_number=to_number, call_sid=call_sid
    )
    agent, dynamic_config = found.agent, found.dynamic_config
    if agent is None and dynamic_config is None:
        logger.warning(
            "inbound_no_agent",
            carrier=carrier,
            routing_target_type=number.routing_target_type,
            routing_target_id=str(number.routing_target_id),
        )
        if number.routing_target_type == "webhook" and number.server_url:
            return Refused("Unable to resolve agent.")
        return Refused("This agent is not available.")

    call = await call_repo.create_call(
        session,
        project_id=number.project_id,
        direction=CallDirection.INBOUND,
        from_number=from_number,
        to_number=to_number,
        # None for an inline agent: it has no row (ADR-0017).
        active_agent_id=agent.id if agent else None,
        provider_call_sid=call_sid,
        metadata_json={
            **({"dynamic_config": dynamic_config} if dynamic_config else {}),
            **(
                {"template_variables": found.template_variables}
                if found.template_variables
                else {}
            ),
            **({"metadata": found.metadata} if found.metadata else {}),
            **(
                {"knowledge_context": found.knowledge_context}
                if found.knowledge_context
                else {}
            ),
        },
        carrier=carrier,
    )
    await _emit_started(session, call, number, agent, bool(dynamic_config), carrier)
    return call


async def _emit_started(
    session: AsyncSession,
    call: CallRow,
    number: PhoneNumberRow,
    agent: Any,
    dynamic: bool,
    carrier: str,
) -> None:
    """`call.initializing` + `call.started` (DB + webhook)."""
    from turncall.events.emit import emit_call_event

    await emit_call_event(
        session,
        call_id=call.id,
        project_id=number.project_id,
        event_type=CallEventType.CALL_INITIALIZING,
        payload={
            "from_number": call.from_number,
            "to_number": call.to_number,
            "direction": "inbound",
            "transport": carrier,
        },
    )
    await emit_call_event(
        session,
        call_id=call.id,
        project_id=number.project_id,
        event_type=CallEventType.CALL_STARTED,
        payload={
            "provider_call_sid": call.provider_call_sid,
            "from_number": call.from_number,
            "to_number": call.to_number,
            # agent_id is carried in the envelope (ADR-0007), not the payload.
            "agent_name": agent.name if agent else "dynamic",
            "dynamic_config": dynamic,
        },
    )
