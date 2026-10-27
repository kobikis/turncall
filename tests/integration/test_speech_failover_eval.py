"""STT and TTS failover over the eval loopback (#223, PRD #212 Seam B).

The real pipeline — `build_call_pipeline`, `CallSession`, the eval transport and
pipecat's harness on the other end of the socket — with only the providers
stubbed: each role's primary is refused with an error that leaves it unusable,
and the call has to carry on through the backup. Hermetic: no key, no model
download, no network.

- STT: the caller's audio reaches a primary that refuses it; the backup
  transcribes, and the agent answers what the caller said.
- TTS: the primary is refused on the first sentence of the reply; the backup
  speaks that sentence — the replay — and the rest.

Either way the iteration's agent errors carry the failover, which is what the
call's `provider.failover` event says on a live call (an eval has no event log).
"""

import wave
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMConfigureOutputFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.errors import ErrorCategory

from turncall.config.settings import Settings
from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget
from turncall.orchestrator import pipeline_builder, pipeline_factory
from turncall.orchestrator.transport_factory import EVAL_SAMPLE_RATE
from turncall.services.credentials import Credentials

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

QUESTION = "What is the capital of France?"
REFUSED = "refused-primary"


class _StubLLM(FrameProcessor):
    """Repeats the caller's last words back, in two sentences."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_tts = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMConfigureOutputFrame):
            self._skip_tts = frame.skip_tts
            return
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        said = str(frame.context.get_messages()[-1].get("content", ""))
        if said == "Start the conversation.":
            return  # the call's own opener: stay quiet until the caller speaks
        for out in (
            LLMFullResponseStartFrame(),
            LLMTextFrame(f"You said: {said.rstrip('?.')}."),
            LLMTextFrame(" Goodbye for now."),
            LLMFullResponseEndFrame(),
        ):
            out.skip_tts = self._skip_tts
            await self.push_frame(out)


class _StubSTT(FrameProcessor):
    """Streaming STT. Refused on the first audio when `refused`; otherwise the
    question, once, after half a second of the caller's audio."""

    def __init__(self, refused: bool) -> None:
        super().__init__()
        self._refused = refused
        self._chunks = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if not isinstance(frame, InputAudioRawFrame):
            return
        if self._refused:
            if self.is_usable:
                await self.push_error(
                    "401 invalid key", category=ErrorCategory.AUTHENTICATION
                )
            return
        self._chunks += 1
        if self._chunks == 25:
            await self.push_frame(TranscriptionFrame(QUESTION, "", "t", finalized=True))


class _StubTTS(FrameProcessor):
    """Says each sentence as it completes. Refused on the first when `refused`."""

    def __init__(self, refused: bool) -> None:
        super().__init__()
        self._refused = refused
        self._text: list[str] = []

    async def _say(self, text: str) -> None:
        if self._refused:
            await self.push_error(
                "401 invalid key", category=ErrorCategory.AUTHENTICATION
            )
            return
        silence = b"\x00\x00" * (EVAL_SAMPLE_RATE // 50)
        await self.push_frame(TTSStartedFrame())
        await self.push_frame(
            TTSAudioRawFrame(
                audio=silence, sample_rate=EVAL_SAMPLE_RATE, num_channels=1
            )
        )
        await self.push_frame(TTSTextFrame(text, text_type="sentence"))
        await self.push_frame(TTSStoppedFrame())

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            if not frame.skip_tts:
                self._text.append(frame.text)
                if frame.text.endswith("."):
                    await self._say("".join(self._text).strip())
                    self._text = []
            return
        if isinstance(frame, TTSSpeakFrame):
            await self._say(frame.text)
            return
        await self.push_frame(frame, direction)


@pytest.fixture
def stub_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every provider stubbed where the factory builds it, so the group, the
    pipeline and the session around them are the real ones."""
    monkeypatch.setattr(
        pipeline_factory,
        "_create_stt_service",
        lambda config, _c, **_k: _StubSTT(config.stt.model == REFUSED),
    )
    monkeypatch.setattr(
        pipeline_factory,
        "_create_tts_service",
        lambda config, _c, **_k: _StubTTS(config.tts.model == REFUSED),
    )
    monkeypatch.setattr(
        pipeline_factory, "_create_llm_stage", lambda *_a, **_k: _StubLLM()
    )
    monkeypatch.setattr(
        pipeline_builder, "load_call_credentials", AsyncMock(return_value=Credentials())
    )


def _target(**blocks: Any) -> ResolvedTarget:
    blob = {"system_prompt": "Echo.", "first_message": None, **blocks}
    return ResolvedTarget(
        project_id=uuid4(),
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )


async def _run(
    definition: dict, target: ResolvedTarget, modality: EvalModality
) -> tuple:
    parsed = scenario_mod.parse(definition, name="speech-failover")
    agent_errors: list[str] = []
    result = await run_iteration(
        parsed=parsed,
        kind=EvalKind.SCRIPTED,
        target=target,
        modality=modality,
        settings=Settings(),
        session_factory=MagicMock(),
        run_id=uuid4(),
        tool_mocks=None,
        timeout_s=60,
        agent_errors=agent_errors,
    )
    return result, agent_errors


def _noise_wav(path: Path) -> str:
    """A second of the caller's 'audio': the stub STT counts it, never reads it."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(EVAL_SAMPLE_RATE)
        wav.writeframes(bytes(range(256)) * (EVAL_SAMPLE_RATE * 2 // 256))
    return str(path)


async def test_a_refused_stt_fails_over_and_the_caller_is_heard(
    stub_providers: None, tmp_path: Path
) -> None:
    target = _target(
        stt={
            "model": REFUSED,
            "failover": [{"provider": "deepgram", "model": "backup"}],
        },
    )
    definition = {
        "user": {"modality": "audio"},
        "turns": [
            {
                "user": QUESTION,
                "audio": _noise_wav(tmp_path / "caller.wav"),
                "expect": [
                    {
                        "event": "llm_response",
                        "text_contains": "capital of France",
                        "within_ms": 30000,
                    }
                ],
            }
        ],
    }

    result, agent_errors = await _run(definition, target, EvalModality.TEXT)

    assert result.passed, ([str(f) for f in result.failures], agent_errors)
    assert any(
        e.startswith(f"stt failed over from deepgram/{REFUSED} to deepgram/backup")
        for e in agent_errors
    ), agent_errors


async def test_a_refused_tts_fails_over_and_the_reply_is_spoken(
    stub_providers: None,
) -> None:
    target = _target(
        # Deepgram is named by its voice (its model field is never sent), and
        # a voice that is not aura-* is replaced, so both are aura names.
        tts={
            "model": REFUSED,
            "voice": "aura-2-refused-en",
            "failover": [
                {"provider": "deepgram", "model": "backup", "voice": "aura-2-backup-en"}
            ],
        },
    )
    definition = {
        # The bot speaks; nothing asserts on `response`, so no transcriber is
        # ever built from this block.
        "judge": {"modality": "audio", "transcription": {"service": "moonshine"}},
        "turns": [
            {
                "user": "hello",
                "expect": [
                    # The first sentence is the one the primary was refused on:
                    # only the replay can have said it.
                    {
                        "event": "tts_response",
                        "text_contains": "You said: hello",
                        "within_ms": 30000,
                    },
                    {
                        "event": "tts_response",
                        "text_contains": "Goodbye for now",
                        "within_ms": 30000,
                    },
                ],
            }
        ],
    }

    result, agent_errors = await _run(definition, target, EvalModality.AUDIO)

    assert result.passed, ([str(f) for f in result.failures], agent_errors)
    assert any(
        e.startswith(
            "tts failed over from deepgram/aura-2-refused-en to deepgram/aura-2-backup-en"
        )
        for e in agent_errors
    ), agent_errors


def _plain_turn(modality: EvalModality) -> dict:
    """One turn the stub LLM answers, asserted on what each modality sees."""
    audio = modality is EvalModality.AUDIO
    expect = {
        "event": "tts_response" if audio else "llm_response",
        "text_contains": "You said: hello",
        "within_ms": 30000,
    }
    definition: dict = {"turns": [{"user": "hello", "expect": [expect]}]}
    if audio:
        definition["judge"] = {
            "modality": "audio",
            "transcription": {"service": "moonshine"},
        }
    return definition


@pytest.mark.parametrize("modality", [EvalModality.TEXT, EvalModality.AUDIO])
async def test_an_iteration_uses_nothing_pipecat_deprecated(
    stub_providers: None, modality: EvalModality, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#375: the eval transport, the harness session's flags and the loopback
    server, over a whole iteration in each modality. Matched on pipecat's own
    deprecation template, so third-party warnings its imports raise are not
    counted."""
    import warnings

    from pipecat.utils import deprecation
    from pipecat.utils.deprecation import DEPRECATION_MESSAGE_RE

    # pipecat warns once per call site per process: a deprecation an earlier
    # test already set off would otherwise pass here unseen.
    monkeypatch.setattr(deprecation, "_warned_sites", set())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result, agent_errors = await _run(_plain_turn(modality), _target(), modality)

    assert result.passed, ([str(f) for f in result.failures], agent_errors)
    deprecated = [
        str(w.message) for w in caught if DEPRECATION_MESSAGE_RE.search(str(w.message))
    ]
    assert deprecated == []


async def test_a_text_turn_reaches_an_agent_with_a_failover_group(
    stub_providers: None,
) -> None:
    """A text turn is RTVI `send-text` with `run_immediately`, which flushes the
    pipeline first. Before pipecat 1.14 that flush never drained a
    `ServiceSwitcher` — every failover group is one — so the turn waited for
    the pipeline to go quiet before it was put to the agent."""
    target = _target(
        tts={
            "voice": "aura-2-primary-en",
            "failover": [{"provider": "deepgram", "voice": "aura-2-backup-en"}],
        },
    )
    definition = _plain_turn(EvalModality.TEXT)
    definition["turns"][0]["expect"][0]["within_ms"] = 5000

    result, agent_errors = await _run(definition, target, EvalModality.TEXT)

    assert result.passed, ([str(f) for f in result.failures], agent_errors)
