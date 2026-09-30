"""Context snapshots: what the model was given, kept on the call record.

The transcript is what was *said*. A snapshot is what the model was *given*:
the system instruction as rendered (template variables and the call-init
`knowledge_context` included), and every context message — retrieved knowledge,
tool calls and their results. One per agent the call ran, because a handoff
clears the context (#177, #180).

Captured twice: just before a handoff clears the context, and when the call
ends. The handoff capture runs after the switch committed, so it must neither
await nor raise (#144) — it copies synchronously and hands the write off.

Never sent in a webhook: served only by `GET /v1/calls/{id}/context`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from loguru import logger

from turncall.orchestrator.observability import _spawn

if TYPE_CHECKING:
    from turncall.orchestrator.pipeline_factory import CallContext

Snapshot = dict[str, Any]

# Live calls only; an entry leaves on `flush_call` at the end of the call.
# ponytail: a pipeline built but never run (its session never started) leaves
# its entry behind; bounded by such failures, add a sweep if they ever pile up.
_REGISTRY: dict[UUID, ContextSnapshotter] = {}


def _agent_or_none(agent_id: UUID | None) -> UUID | None:
    from turncall.orchestrator.pipeline_factory import DYNAMIC_AGENT_ID

    # An inline agent has no row (ADR-0017): null, never the sentinel.
    return None if agent_id == DYNAMIC_AGENT_ID else agent_id


def _json_safe(messages: Any) -> list[Any]:
    """A detached, JSON-storable copy. Anything the encoder can't take becomes
    its string form rather than failing the snapshot."""
    copied: list[Any] = json.loads(json.dumps(list(messages), default=str))
    return copied


class ContextSnapshotter:
    """Tracks the current agent segment and writes each one's snapshot."""

    def __init__(
        self,
        call_context: CallContext,
        *,
        context: Any,
        system_instruction: str,
        store_context: bool,
        write: Callable[[Snapshot], Awaitable[None]] | None = None,
    ) -> None:
        self._call_context = call_context
        self._context = context
        self._write = write or self._write_row
        self._segment = 0
        self._agent_id = _agent_or_none(call_context.agent_id)
        self._system_instruction = system_instruction
        self._store = store_context

    def _snapshot(self) -> Snapshot:
        return {
            "segment": self._segment,
            "agent_id": self._agent_id,
            "system_instruction": self._system_instruction,
            "messages": _json_safe(self._context.get_messages()),
        }

    def capture_for_handoff(
        self, *, agent_id: UUID | None, system_instruction: str, store_context: bool
    ) -> None:
        """Close the current segment and open the target agent's. Sync, and
        never raises: it runs inside a committed handoff."""
        try:
            if self._store:
                _spawn(self._write(self._snapshot()))
        except Exception:
            logger.exception(
                "context_snapshot_capture_error",
                call_id=str(self._call_context.call_id),
            )
        self._segment += 1
        self._agent_id = _agent_or_none(agent_id)
        self._system_instruction = system_instruction
        self._store = store_context

    async def flush(self) -> None:
        """Write the last segment. Called once, when the call ends."""
        if not self._store:
            return
        try:
            await self._write(self._snapshot())
        except Exception:
            logger.exception(
                "context_snapshot_write_error",
                call_id=str(self._call_context.call_id),
            )

    async def _write_row(self, snapshot: Snapshot) -> None:
        from turncall.storage.repositories import context_snapshot_repo

        call_id = self._call_context.call_id
        try:
            async with self._call_context.session_factory() as session:
                await context_snapshot_repo.create_snapshot(
                    session, call_id=call_id, **snapshot
                )
                await session.commit()
        except Exception:
            logger.exception("context_snapshot_write_error", call_id=str(call_id))


def register(
    call_context: CallContext, context: Any, system_instruction: str, config: Any
) -> ContextSnapshotter | None:
    """Start snapshotting a live call. An eval has no call row (ADR-0018)."""
    if call_context.is_eval:
        return None
    snapshotter = ContextSnapshotter(
        call_context,
        context=context,
        system_instruction=system_instruction,
        store_context=config.observability.store_context,
    )
    _REGISTRY[call_context.call_id] = snapshotter
    return snapshotter


def for_call(call_id: UUID) -> ContextSnapshotter | None:
    return _REGISTRY.get(call_id)


def discard(call_id: UUID) -> None:
    _REGISTRY.pop(call_id, None)


async def flush_call(call_id: UUID) -> None:
    """Write a call's final segment and forget it. A no-op for an unknown call."""
    snapshotter = _REGISTRY.pop(call_id, None)
    if snapshotter is not None:
        await snapshotter.flush()
