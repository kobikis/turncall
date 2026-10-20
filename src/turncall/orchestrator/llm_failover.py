"""LLM failover (#222): the backup providers a call's LLM moves to.

pipecat's `LLMSwitcher` with its failover strategy does the switching: only an
error that leaves the active service unusable moves the call on, in list
order, and a failed service stays unusable for the rest of the call — so there
is no switching back, which would change the voice of the conversation twice.

What it does not do is answer the turn that failed. The context frame died
with the service it was handed to, so without a replay the caller's last
sentence goes unanswered and the call sits in silence until they speak again.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.pipeline.llm_switcher import LLMSwitcher
from pipecat.pipeline.service_switcher import ServiceSwitcherStrategyFailover
from pipecat.processors.frame_processor import FrameDirection

from turncall.orchestrator.failover import _GROUP_ATTR, FailoverGroup


class FailoverLLM(FailoverGroup, LLMSwitcher[ServiceSwitcherStrategyFailover]):
    """An agent's LLM and its backups, in order. `llms` are `(label, service)`.

    Event handlers: ``on_failover(switcher, from_label, to_label, error_frame)``.
    """

    role = "llm"

    def __init__(self, llms: list[tuple[str, Any]]) -> None:
        super().__init__(
            [service for _, service in llms], ServiceSwitcherStrategyFailover
        )
        self._init_group(llms)
        self._last_context: LLMContextFrame | None = None

    def get_full_model_name(self) -> str | None:
        """The model answering now, for the transcript (observability.py)."""
        getter = getattr(self.active_llm, "get_full_model_name", None)
        return getter() if getter else None

    @staticmethod
    def group_of(llm: Any) -> Any:
        """The group a member LLM belongs to, or the LLM itself if none."""
        group = getattr(llm, _GROUP_ATTR, None)
        return group if isinstance(group, FailoverLLM) else llm

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, LLMContextFrame):
            self._last_context = frame
        await super().process_frame(frame, direction)

    # --- What pipecat's backend worker asks of its LLM (1.13, #367) ---
    # An `openai_live` client delegate is the agent's own llm, so with backups
    # it is this group. `BackendLLMWorker` takes an `LLMService` and, since
    # 1.13, asks it whether it can be a backend, sets its reasoning default,
    # appends the pairing instruction and cancels its calls when let go of.
    # Each is a member's to answer, and every member must hear it: a backup
    # the call fails over to is the same delegate.

    def llm_with_backend_role_objection(self, role: Any) -> str | None:
        objections = (llm.llm_with_backend_role_objection(role) for llm in self.llms)
        return next((o for o in objections if o), None)

    @property
    def settings(self) -> _MemberSettings:
        return _MemberSettings(self)

    def append_system_instruction(self, instruction: str) -> None:
        for llm in self.llms:
            llm.append_system_instruction(instruction)

    async def cancel_function_calls(self, *, reason: str = "cancelled") -> list[Any]:
        cancelled: list[Any] = []
        for llm in self.llms:
            cancelled += await llm.cancel_function_calls(reason=reason)
        return cancelled

    async def _replay(self, service: Any) -> None:
        if self._last_context is not None:
            # ponytail: a provider that streamed half a reply before dying gets
            # it said again in full; the failures this is for (401, unknown
            # model, outage) die before the first token.
            await self.queue_frame(replace(self._last_context))


class _MemberSettings:
    """A group's settings: read from the member answering now; an update or a
    field set reaches every member."""

    def __init__(self, group: FailoverLLM) -> None:
        object.__setattr__(self, "_group", group)

    def apply_update(self, delta: Any) -> dict[str, Any]:
        changed: dict[str, Any] = {}
        for llm in self._group.llms:
            changed |= llm.settings.apply_update(delta)
        return changed

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            # Only this proxy's own, never a member's: and no recursion when
            # it is copied without `__init__`.
            raise AttributeError(name)
        return getattr(self._group.active_llm.settings, name)

    def __setattr__(self, name: str, value: Any) -> None:
        for llm in self._group.llms:
            setattr(llm.settings, name, value)
