"""Stub STT, LLM and TTS: instant, deterministic, scripted.

Each is a real pipecat service subclass, so everything around it in the
pipeline — the turn strategies, the tool bridge, the hangup gate, the
transcript taps, the recorder — runs exactly as on a call. Only the network
hop to a provider is gone.

A `Script` is shared, immutable data; every pipeline build gets services with
their own cursor into it, so concurrent calls each run the whole script.
"""

from __future__ import annotations

import audioop
import math
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    FunctionCallFromLLM,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601

# Said when the script has run out: visible in a transcript, never a crash.
EXHAUSTED = "[stub llm: no scripted reply]"
UNHEARD = "[stub stt: no scripted line]"


@dataclass(frozen=True)
class ToolCall:
    """The model calling a tool, with these arguments."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


# One inference's answer: text to say, a tool call, or both in order.
Reply = str | ToolCall | tuple[str | ToolCall, ...]


@dataclass(frozen=True)
class Script:
    """What the caller says and what the agent answers.

    `caller[n]` is what the STT hears for the caller's n-th burst of speech.
    `agent[n]` is the LLM's answer to its n-th inference — a tool result is an
    inference of its own, so a tool call and the reply after it are two
    entries. A cascade agent's `first_message` is spoken by the TTS directly
    and takes no entry; without one, the opening is `agent[0]`.
    """

    caller: tuple[str, ...] = ()
    agent: tuple[Reply, ...] = ()
    # Agent speech length per word. Real TTS is ~400ms a word; tests want less.
    tts_ms_per_word: int = 60


class StubSTT(STTService):
    """Transcribes each burst of caller audio as the next scripted line.

    A burst is audio above `threshold` RMS, ended by `end_silence_ms` of
    quiet. The harness's caller speaks a tone, which Silero VAD does not take
    for speech, so turns are driven by these transcripts alone — the
    deterministic path. A test needing VAD-driven turns must send speech.
    """

    def __init__(
        self, lines: Sequence[str], *, threshold: int = 500, end_silence_ms: int = 100
    ) -> None:
        super().__init__(ttfs_p99_latency=0.0)
        self._lines = iter(lines)
        self._threshold = threshold
        self._end_silence_ms = end_silence_ms
        self._in_burst = False
        self._quiet_ms = 0.0

    def can_generate_metrics(self) -> bool:
        return True

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if audioop.rms(audio, 2) >= self._threshold:
            self._in_burst, self._quiet_ms = True, 0.0
        elif self._in_burst:
            self._quiet_ms += len(audio) / 2 / self.sample_rate * 1000
            if self._quiet_ms >= self._end_silence_ms:
                self._in_burst = False
                text = next(self._lines, UNHEARD)
                yield TranscriptionFrame(
                    text, self._user_id, time_now_iso8601(), finalized=True
                )
                return
        yield None


class StubLLM(LLMService):
    """Answers its n-th inference with the script's n-th reply."""

    def __init__(self, replies: Sequence[Reply]) -> None:
        super().__init__()
        self._replies = iter(replies)
        self._calls = 0

    def can_generate_metrics(self) -> bool:
        # pipeline_builder finds the LLM to register tools on by this; False
        # would leave every tool call unhandled.
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        await self.push_frame(LLMFullResponseStartFrame())
        await self.start_ttfb_metrics()
        reply = next(self._replies, EXHAUSTED)
        await self.stop_ttfb_metrics()
        calls = []
        for part in reply if isinstance(reply, tuple) else (reply,):
            if isinstance(part, ToolCall):
                calls.append(self._function_call(part, frame.context))
            else:
                await self._push_llm_text(part)
        if calls:
            await self.run_function_calls(calls)
        await self.push_frame(LLMFullResponseEndFrame())

    def _function_call(self, call: ToolCall, context: Any) -> FunctionCallFromLLM:
        self._calls += 1
        return FunctionCallFromLLM(
            function_name=call.name,
            tool_call_id=f"stub_call_{self._calls}",
            arguments=call.arguments,
            context=context,
        )


class StubTTS(TTSService):
    """Speaks any text as a 440 Hz tone, `ms_per_word` long per word."""

    def __init__(self, *, ms_per_word: int) -> None:
        super().__init__(push_start_frame=True, push_stop_frames=True)
        self._ms_per_word = ms_per_word

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        audio = tone(len(text.split()) * self._ms_per_word, self.sample_rate)
        for start in range(0, len(audio), self.chunk_size):
            await self.stop_ttfb_metrics()
            yield TTSAudioRawFrame(
                audio[start : start + self.chunk_size],
                self.sample_rate,
                1,
                context_id=context_id,
            )


def tone(ms: int, sample_rate: int, *, hz: int = 440, amplitude: int = 8000) -> bytes:
    """PCM16 mono sine. Silero VAD scores it below 0.1, so it is never speech."""
    count = max(1, sample_rate * ms // 1000)
    return b"".join(
        int(amplitude * math.sin(2 * math.pi * hz * i / sample_rate)).to_bytes(
            2, "little", signed=True
        )
        for i in range(count)
    )


Setter = Callable[[Any, str, Any], None]


def install_stub_providers(script: Script, set_attr: Setter = setattr) -> None:
    """Make every cascade pipeline built from now on use the stubs.

    Replaces the three provider factories in `orchestrator/pipeline_factory`,
    which is where every voice transport builds its services. Pass pytest's
    `monkeypatch.setattr` to undo it after the test; the default is for a
    server process that runs on stubs for its whole life (#200's load
    generator) — no production code path knows the stubs exist.

    Not covered: S2S pipelines, post-call analysis and takeaways, the
    voicemail classifier and knowledge-base embeddings — each still reaches
    for its real provider, so a harness agent keeps them off.
    """
    from turncall.orchestrator import pipeline_factory

    def stt(*_args: Any, **_kwargs: Any) -> StubSTT:
        return StubSTT(script.caller)

    def llm(*_args: Any, **_kwargs: Any) -> StubLLM:
        return StubLLM(script.agent)

    def tts(*_args: Any, **_kwargs: Any) -> StubTTS:
        return StubTTS(ms_per_word=script.tts_ms_per_word)

    for name, stub in (
        ("_create_stt_service", stt),
        ("_create_llm_service", llm),
        ("_create_tts_service", tts),
    ):
        if not hasattr(pipeline_factory, name):
            # A renamed factory would otherwise leave real providers in place.
            raise AttributeError(f"pipeline_factory.{name} is gone; update the harness")
        set_attr(pipeline_factory, name, stub)
    logger.info("harness: stub STT/LLM/TTS installed")
