"""What a voice call starts with: its row and its first events (#487).

Every voice call is written the same way, whatever answered it: the call row
— an inline agent's raw config as `dynamic_config` and no agent id
(ADR-0017) — and `call.initializing` + `call.started`, stored and sent. An
opened call's are written by `call_opening.open_call` in one go; an outbound
dial writes its row before the carrier is asked and its start events once
the carrier answered (`write_call_row`, then `write_call_start`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import CallDirection, CallEventType
from turncall.storage.models import CallRow
from turncall.storage.repositories import call_repo


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

    @property
    def transport_call_ref(self) -> dict[str, str]:
        """The transport's id for the call under its own name
        (`whatsapp_call_id`), as the row and `call.started` have always
        carried it; empty for a call no such transport carries."""
        if not (self.transport and self.transport_call_id):
            return {}
        return {f"{self.transport}_call_id": self.transport_call_id}

    @property
    def inline_agent_name(self) -> str | None:
        """What `call.started` names an inline agent, which has no row: a
        phone call's has always said "dynamic", WhatsApp's null (#487 changes
        neither)."""
        return None if self.transport else "dynamic"

    def row_json(self) -> dict[str, Any]:
        """The edge's keys on the call row's metadata: its own, the transport
        a call no carrier carries arrived on, and that transport's id."""
        transport = {"transport": self.transport} if self.transport else {}
        return {**self.row_metadata, **transport, **self.transport_call_ref}


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
class CallStart:
    """What a call's row and start events are written from: an opened
    call's, or an outbound dial's, whose agent is accepted only once its
    media stream connects (#481). `agent_id` None for an inline agent;
    `agent_name` is what `call.started` names a stored one."""

    call_id: UUID
    project_id: UUID
    agent_id: UUID | None
    agent_name: str | None
    metadata: CallMetadata
    parties: CallParties
    handles: EdgeHandles
    direction: CallDirection


async def write_call_row(session: AsyncSession, start: CallStart) -> CallRow:
    """The call's row, flushed, not committed. An outbound dial writes it
    before the carrier is asked: a row that cannot be sealed (#338) then
    dials nothing."""
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
        metadata_json={**start.metadata.row_json(), **handles.row_json()},
        carrier=handles.carrier,
    )


async def write_call_start(session: AsyncSession, start: CallStart) -> None:
    """`call.initializing` + `call.started`, stored and sent. The carrier's
    id for the call is the one `start` carries."""
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
            "agent_name": (
                start.agent_name
                if start.agent_name is not None
                else handles.inline_agent_name
            ),
            "dynamic_config": bool(start.metadata.dynamic_config),
            "direction": start.direction.value,
            "transport": handles.transport_name,
            **handles.transport_call_ref,
        },
    )
