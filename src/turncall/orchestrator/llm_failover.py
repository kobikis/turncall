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

from pipecat.frames.frames import ErrorFrame, Frame, LLMContextFrame
from pipecat.pipeline.llm_switcher import LLMSwitcher
from pipecat.pipeline.service_switcher import ServiceSwitcherStrategyFailover
from pipecat.processors.frame_processor import FrameDirection

# Set on each member so a handler that only holds the member (a tool call's
# `params.llm`) can reach the whole group.
_GROUP_ATTR = "_turncall_failover_group"


class FailoverLLM(LLMSwitcher[ServiceSwitcherStrategyFailover]):
    """An agent's LLM and its backups, in order. `llms` are `(label, service)`.

    Event handlers: ``on_failover(switcher, from_label, to_label, error_frame)``.
    """

    def __init__(self, llms: list[tuple[str, Any]]) -> None:
        services = [service for _, service in llms]
        super().__init__(services, ServiceSwitcherStrategyFailover)
        self._labels = {id(service): label for label, service in llms}
        self._last_context: LLMContextFrame | None = None
        # Members this call has given up on. pipecat makes a service usable
        # again on any settings change, and a handoff's prompt update reaches
        # every member — without this, the next failover could wrap round to
        # the dead primary.
        self._failed: set[int] = set()
        for service in services:
            setattr(service, _GROUP_ATTR, self)
            service.add_event_handler("on_usable_changed", self._keep_failed_down)
        self._register_event_handler("on_failover")

    async def _keep_failed_down(self, service: Any, is_usable: bool) -> None:
        if is_usable and id(service) in self._failed:
            await service.set_usable(False)

    def label(self, service: Any) -> str:
        """`provider/model` of a member, as the call's event log names it."""
        return self._labels[id(service)]

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

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        before = self.active_llm
        await super().push_frame(frame, direction)
        after = self.active_llm
        if after is before or not isinstance(frame, ErrorFrame):
            return
        self._failed.add(id(before))
        await self._call_event_handler(
            "on_failover", self.label(before), self.label(after), frame
        )
        if self._last_context is not None:
            # ponytail: a provider that streamed half a reply before dying gets
            # it said again in full; the failures this is for (401, unknown
            # model, outage) die before the first token.
            await self.queue_frame(replace(self._last_context))
