"""An inbound phone call, from a resolved number to a call row (ADR-0022).

Every carrier's inbound edge verifies its own webhook, finds the dialled
number, and then does the same thing: take a live-call slot (#224), resolve
the agent (call-init, A/B weights, or the bound agent), create the call row on
its own carrier, and record `call.initializing` + `call.started`. That shared
middle is here; how the call is then answered — or refused busy — (TwiML, a
Call Control command) stays at the edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import CallDirection, CallEventType
from turncall.services.limits import admit_inbound_call, release_live_call
from turncall.storage.models import CallRow, PhoneNumberRow
from turncall.storage.repositories import agent_repo, call_repo


@dataclass(frozen=True)
class InboundRefusal:
    """The call is not taken. `busy`: the project is at its live-call limit
    and the carrier must refuse the call unanswered (answering only to say no
    bills minutes). Otherwise no agent can take it, and `message` is what the
    caller is told."""

    message: str
    busy: bool = False


AT_LIVE_CALL_LIMIT = InboundRefusal("The project is at its live-call limit.", busy=True)


@dataclass(frozen=True)
class _Resolved:
    agent: Any = None
    dynamic_config: dict[str, Any] | None = None
    template_variables: dict[str, str] | None = None
    metadata: dict[str, Any] | None = None
    knowledge_context: str | None = None


async def _resolve_agent(
    session: AsyncSession,
    number: PhoneNumberRow,
    *,
    from_number: str,
    to_number: str,
    provider_call_sid: str,
    call_id: UUID,
) -> _Resolved | InboundRefusal:
    if number.routing_target_type == "webhook" and number.server_url:
        from turncall.events.server_events import send_call_init
        from turncall.services.call_init_resolver import resolve_call_init

        response = await send_call_init(
            number.server_url,
            call_id=str(call_id),
            from_number=from_number,
            to_number=to_number,
            call_sid=provider_call_sid,
            transport_type="inboundPhoneCall",
            secret=number.server_url_secret,
        )
        if response.success and response.data:
            result = await resolve_call_init(session, response.data)
            if result.agent is not None or result.dynamic_config is not None:
                return _Resolved(
                    result.agent,
                    result.dynamic_config,
                    result.template_variables,
                    result.metadata,
                    result.knowledge_context,
                )
        logger.warning("inbound_server_url_no_agent", server_url=number.server_url)
        return InboundRefusal("Unable to resolve agent.")

    agent = None
    if number.routing_target_type == "agent" and number.routing_target_id:
        agent_id = number.routing_target_id
        if number.routing_weights:
            # A/B weighted routing: pick agent by caller number hash
            from turncall.services.weighted_routing import pick_agent_by_weight

            agent_id = pick_agent_by_weight(number.routing_weights, from_number)
            logger.info(
                "ab_routing_selected",
                phone_number=to_number,
                caller=from_number,
                selected_agent_id=str(agent_id),
            )
        agent = await agent_repo.get_agent_by_id(session, agent_id)
    if agent is None:
        logger.warning(
            "inbound_no_agent",
            routing_target_type=number.routing_target_type,
            routing_target_id=str(number.routing_target_id),
        )
        return InboundRefusal("This agent is not available.")
    return _Resolved(agent)


async def open_inbound_call(
    session: AsyncSession,
    number: PhoneNumberRow,
    *,
    carrier: str,
    from_number: str,
    to_number: str,
    provider_call_sid: str,
) -> CallRow | InboundRefusal:
    """Admit the call, resolve the number's agent and open the call on `carrier`.

    Returns the call row (flushed, not committed) with `call.initializing` and
    `call.started` recorded, or why it is not taken. The live-call slot it took
    is the media stream's to hold from then on (`hold_live_call`).
    """
    # Before call-init: a call refused at the carrier asks nobody for an agent.
    # The slot is taken under the call's id before the row exists (#224);
    # `admit_inbound_call` has already announced `call.rejected` when it says no.
    call_id = uuid4()
    if not await admit_inbound_call(
        session,
        project_id=number.project_id,
        call_id=call_id,
        transport=carrier,
        from_number=from_number,
        to_number=to_number,
        provider_call_id=provider_call_sid,
    ):
        return AT_LIVE_CALL_LIMIT
    resolved = await _resolve_agent(
        session,
        number,
        from_number=from_number,
        to_number=to_number,
        provider_call_sid=provider_call_sid,
        call_id=call_id,
    )
    if isinstance(resolved, InboundRefusal):
        await release_live_call(number.project_id, call_id)
        return resolved

    agent = resolved.agent
    # Not agent.id unguarded: a call whose agent came from call-init as an
    # inline config has no agent row (ADR-0017).
    agent_id: UUID | None = agent.id if agent else None
    metadata: dict[str, Any] = {
        **(
            {"dynamic_config": resolved.dynamic_config}
            if resolved.dynamic_config
            else {}
        ),
        **(
            {"template_variables": resolved.template_variables}
            if resolved.template_variables
            else {}
        ),
        **({"metadata": resolved.metadata} if resolved.metadata else {}),
        **(
            {"knowledge_context": resolved.knowledge_context}
            if resolved.knowledge_context
            else {}
        ),
    }
    call = await call_repo.create_call(
        session,
        project_id=number.project_id,
        call_id=call_id,
        direction=CallDirection.INBOUND,
        from_number=from_number,
        to_number=to_number,
        active_agent_id=agent_id,
        provider_call_sid=provider_call_sid,
        metadata_json=metadata,
        carrier=carrier,
    )

    from turncall.events.emit import emit_call_event

    await emit_call_event(
        session,
        call_id=call.id,
        project_id=number.project_id,
        event_type=CallEventType.CALL_INITIALIZING,
        payload={
            "from_number": from_number,
            "to_number": to_number,
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
            "provider_call_sid": provider_call_sid,
            "from_number": from_number,
            "to_number": to_number,
            # agent_id is carried in the envelope (ADR-0007), not the payload.
            "agent_name": agent.name if agent else "dynamic",
            "dynamic_config": bool(resolved.dynamic_config),
        },
    )
    logger.info(
        "inbound_call_opened",
        carrier=carrier,
        call_id=str(call.id),
        agent_id=str(agent_id) if agent_id else "dynamic",
        provider_call_sid=provider_call_sid,
    )
    return call
