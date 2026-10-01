"""The assistant transcript records what was said, not what was meant (#188).

The tap sits before the TTS. When the TTS can no longer work (a refused key)
nothing the agent "says" is heard, and a transcript entry for it would claim a
greeting the caller never got.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from turncall.orchestrator.observability import AssistantTranscriptTapProcessor

pytestmark = pytest.mark.unit


def _tap(tts_usable: bool | None):
    factory = MagicMock()
    ctx = SimpleNamespace(call_id=uuid4(), project_id=uuid4(), session_factory=factory)
    tts = None if tts_usable is None else SimpleNamespace(is_usable=tts_usable)
    return AssistantTranscriptTapProcessor(call_context=ctx, tts_service=tts), factory


async def test_nothing_is_recorded_when_the_tts_cannot_speak() -> None:
    tap, factory = _tap(tts_usable=False)
    await tap._log_transcript("Buona sera, thank you for calling Nonna!")
    factory.assert_not_called()


async def test_a_working_tts_records_as_before() -> None:
    tap, factory = _tap(tts_usable=True)
    await tap._log_transcript("Buona sera")
    factory.assert_called_once()


async def test_s2s_has_no_tts_and_records_as_before() -> None:
    tap, factory = _tap(tts_usable=None)
    await tap._log_transcript("Buona sera")
    factory.assert_called_once()
