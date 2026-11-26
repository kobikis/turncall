"""An appended message reaches a realtime session and is answered (#388, #390).

Two appends that matter go in at the pipeline's source: the S2S idle check-in
(`CallSession._speak_idle_prompt`) and an eval's typed caller turn (RTVI's
`send-text`). Up to pipecat 1.16 the user aggregator took them, pushed the
context downstream, and the Grok and OpenAI Realtime services read that as
nothing to answer: Grok said nothing (measured live,
`tests/live/test_live_xai_tools.py`), and TurnCall carried the append around
the user aggregator (`realtime_appends`, deleted with 1.17). Nova Sonic read a
downstream context for function results only, so its check-in never reached
the model either.

Since 1.17 the user aggregator in realtime mode passes the append on, the
service takes the messages in from the frame, and the assistant aggregator
records them and asks for the response. These tests run that path through the
processors the pipeline factory builds between the two aggregators, with only
the service's socket replaced: they fail if pipecat stops answering an append
on any of these services, or if a TurnCall processor stops passing it on.
gpt-live-1 is the exception: it reads no context after its first and ignores
the frame, so its check-in goes through `say()` instead.
"""

import inspect
import json
import pkgutil
import uuid
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import LLMMessagesAppendFrame, SpeechOutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregator,
    LLMUserAggregator,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.tests.utils import run_test

import turncall.orchestrator
from tests.conftest import creds
from turncall.domain.models import AgentConfig
from turncall.domain.providers import S2S_PROVIDERS
from turncall.orchestrator.pipeline_factory import CallContext, create_pipeline

pytestmark = pytest.mark.unit

_CHECK_IN = {"role": "user", "content": "The caller has gone quiet. Check in."}

# The OpenAI Realtime protocol: Grok Voice, OpenAI Realtime, Eleven Realtime.
_REALTIME_PROTOCOL = ["xai", "openai", "elevenlabs"]

_CREDS = creds(
    "sk-test", google="g", xai="xai-key", elevenlabs="xi", heygen="k", tavus="k"
)
# Nova Sonic resolves its credentials at build; static keys, nothing reached.
_AWS = {"aws": {"access_key_id": "AKIA", "secret_access_key": "s"}}
# The Avatar each provider shows, with the id its build needs.
_AVATARS = {"heygen": {"avatar_id": "a1"}, "tavus": {"replica_id": "r1"}}


def _context(*, records: bool) -> CallContext:
    """A call with no platform behind it (the website demo's), or one that
    records: the build only decides by it, and never opens the session."""

    def _never_opened() -> Any:
        raise AssertionError("building a pipeline opens no database session")

    return CallContext(
        call_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        call_sid="webrtc",
        stream_sid="webrtc",
        session_factory=_never_opened if records else None,  # type: ignore[arg-type]
    )


def _build(
    config: AgentConfig, *, records: bool = False, avatar: bool = False
) -> list[Any]:
    return create_pipeline(
        config=config,
        transport=MagicMock(),
        call_context=_context(records=records),
        pipecat_settings=SimpleNamespace(),
        credentials=_CREDS,
        avatar_enabled=avatar,
    ).processors


def _between_the_aggregators(processors: list[Any]) -> list[FrameProcessor]:
    """From the user aggregator to the assistant aggregator, both included:
    everything an append passes through."""
    first = next(
        i for i, p in enumerate(processors) if isinstance(p, LLMUserAggregator)
    )
    last = next(
        i for i, p in enumerate(processors) if isinstance(p, LLMAssistantAggregator)
    )
    # The transport's output is the test's MagicMock; it passes every frame.
    return [p for p in processors[first : last + 1] if isinstance(p, FrameProcessor)]


def _default_pipeline(provider: str) -> list[FrameProcessor]:
    config = AgentConfig.model_validate(
        {"pipeline_mode": "s2s", "s2s": {"provider": provider}, **_AWS}
    )
    return _between_the_aggregators(_build(config))


def _service(processors: list[FrameProcessor]) -> Any:
    return next(p for p in processors if isinstance(p, LLMService))


async def _not_connected(*_args: Any, **_kwargs: Any) -> None:
    pass


def _session_under_way(processors: list[FrameProcessor], sent: Any) -> Any:
    """The realtime-protocol service with its session already set up — a call
    under way — sending each client event to `sent` instead of the socket."""
    service = _service(processors)
    service._connect = service._disconnect = _not_connected
    service._ws_send = sent
    service._api_session_ready = True
    service._llm_needs_conversation_setup = False
    service._context = processors[0].context
    return service


async def _append(processors: list[FrameProcessor], *, run_llm: bool) -> None:
    await run_test(
        Pipeline(processors),
        frames_to_send=[LLMMessagesAppendFrame(messages=[_CHECK_IN], run_llm=run_llm)],
    )


async def _sent_by_a_realtime_session(provider: str, *, run_llm: bool) -> list[str]:
    """What the service sends xAI / OpenAI / ElevenLabs for one append."""
    processors = _default_pipeline(provider)
    sent: list[str] = []

    async def _record(message: dict[str, Any]) -> None:
        sent.append(message["type"])

    _session_under_way(processors, _record)
    await _append(processors, run_llm=run_llm)
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", _REALTIME_PROTOCOL)
async def test_the_service_sends_the_message_and_asks_for_a_reply(
    provider: str,
) -> None:
    sent = await _sent_by_a_realtime_session(provider, run_llm=True)

    assert sent == ["conversation.item.create", "response.create"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", _REALTIME_PROTOCOL)
async def test_a_message_not_to_run_is_taken_in_unanswered(provider: str) -> None:
    sent = await _sent_by_a_realtime_session(provider, run_llm=False)

    assert sent == ["conversation.item.create"]


@pytest.mark.asyncio
async def test_gemini_live_takes_it_in_and_completes_the_turn() -> None:
    """Gemini Live used to read the message out of the downstream context; it
    now takes it from the frame and answers on the assistant aggregator's
    upstream context, as the other services do."""
    processors = _default_pipeline("google")
    service = _service(processors)
    session = MagicMock(
        send_client_content=AsyncMock(), send_realtime_input=AsyncMock()
    )
    service._connect = service._disconnect = _not_connected
    service._session = session
    service._context = processors[0].context

    await _append(processors, run_llm=True)

    completes = [
        c.kwargs["turn_complete"] for c in session.send_client_content.await_args_list
    ]
    assert completes == [False, True]


async def _nova_sonic_text(*, run_llm: bool) -> list[tuple[bool, str]]:
    """What Nova Sonic's bidirectional stream is sent for one append, its
    audio input already started — a call under way: each text content's
    `interactive` flag and its text."""
    processors = _default_pipeline("aws")
    service = _service(processors)
    events: list[dict[str, Any]] = []

    async def _send(chunk: Any) -> None:
        events.append(json.loads(chunk.value.bytes_)["event"])

    service._start_connecting = service._disconnect = _not_connected
    service._stream = SimpleNamespace(input_stream=SimpleNamespace(send=_send))
    service._prompt_name = "prompt"
    service._audio_input_started = True
    service._context = processors[0].context

    await _append(processors, run_llm=run_llm)

    interactive = [
        e["contentStart"]["interactive"] for e in events if "contentStart" in e
    ]
    texts = [e["textInput"]["content"] for e in events if "textInput" in e]
    return list(zip(interactive, texts, strict=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("run_llm", [True, False])
async def test_nova_sonic_takes_it_in_as_text(run_llm: bool) -> None:
    """Interactive text when the append asks for a reply, which Nova Sonic
    answers as it would the caller speaking; plain text to take in otherwise."""
    assert await _nova_sonic_text(run_llm=run_llm) == [(run_llm, _CHECK_IN["content"])]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", _REALTIME_PROTOCOL)
async def test_it_is_recorded_once_in_the_context(provider: str) -> None:
    """By the assistant aggregator; the user aggregator only passes it on.
    (Eleven Realtime's context opens on its greeting turn, #468.)"""
    processors = _default_pipeline(provider)
    context = _session_under_way(processors, AsyncMock())._context

    await _append(processors, run_llm=True)

    assert context.get_messages().count(_CHECK_IN) == 1
    assert context.get_messages()[-1] == _CHECK_IN


# --- Every stage between the aggregators passes it on ------------------------
#
# The stages are not listed here: they are what the factory builds for each S2S
# provider with every optional stage turned on — the caller speech timer
# (`server_vad` on a call that records), the Avatar of each provider behind its
# speech cutter, the ambience's speech judge, resampling from a phone's 8 kHz.
# `test_every_turncall_processor_is_built_somewhere` fails on a processor these
# builds do not reach, so a new one is covered the day it is added.


def _every_stage_on(provider: str, avatar: str) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "pipeline_mode": "s2s",
            "s2s": {"provider": provider, "turn_detection": "server_vad"},
            "transport": "webrtc",
            "avatar": {"enabled": True, "provider": avatar, **_AVATARS[avatar]},
            "ambience": {"sound": str(uuid.uuid4())},
            **_AWS,
        }
    )


def _s2s_stages(provider: str, avatar: str) -> list[FrameProcessor]:
    """TurnCall's stages between the aggregators, every optional one on: those
    a call with no platform builds, then those only a recording call adds."""
    config = _every_stage_on(provider, avatar)
    built = _between_the_aggregators(_build(config, avatar=True))
    kinds = {type(p) for p in built}
    recording = _between_the_aggregators(_build(config, records=True, avatar=True))
    stages = built + [p for p in recording if type(p) not in kinds]
    return [
        p
        for p in stages
        if not isinstance(p, (LLMUserAggregator, LLMAssistantAggregator, LLMService))
    ]


@pytest.fixture
def avatar_sessions(monkeypatch, heygen_session) -> None:
    """Each Avatar's provider grants its session; nothing is reached. Tavus
    joins a Daily room through pipecat's transport client, faked here."""
    from pipecat.transports.tavus.transport import TavusTransportClient

    for name in ("setup", "join", "start_send_task", "cleanup"):
        monkeypatch.setattr(TavusTransportClient, name, AsyncMock())


@pytest.fixture(autouse=True)
async def _close_shared_http_session():
    yield
    from turncall.adapters.aiohttp_client import close_aiohttp_session

    await close_aiohttp_session()


@pytest.mark.asyncio
@pytest.mark.parametrize("avatar", list(_AVATARS))
@pytest.mark.parametrize("provider", S2S_PROVIDERS)
async def test_every_s2s_stage_passes_it_on(
    provider: str, avatar: str, avatar_sessions: None
) -> None:
    stopped: list[str] = []
    for stage in _s2s_stages(provider, avatar):
        append = LLMMessagesAppendFrame(messages=[_CHECK_IN], run_llm=True)
        down, _ = await run_test(stage, frames_to_send=[append])
        carried = [f for f in down if not isinstance(f, SpeechOutputAudioRawFrame)]
        if carried != [append]:
            stopped.append(type(stage).__name__)
    assert stopped == []


def _orchestrator_modules() -> Iterator[ModuleType]:
    import importlib

    package = turncall.orchestrator
    for module in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
        yield importlib.import_module(module.name)


def _turncall_processors() -> set[type]:
    """Every frame processor TurnCall defines, but the speech services: the
    realtime ones are driven above, and cascade's never sit on S2S."""
    speech = (LLMService, STTService, TTSService)
    return {
        kind
        for module in _orchestrator_modules()
        for _, kind in inspect.getmembers(module, inspect.isclass)
        if kind.__module__ == module.__name__
        and issubclass(kind, FrameProcessor)
        and not issubclass(kind, speech)
    }


def _walk(processors: list[Any]) -> Iterator[Any]:
    """Each processor, and those a group (a failover switcher) holds."""
    for processor in processors:
        yield processor
        yield from _walk(getattr(processor, "processors", None) or [])


def _cascade_every_stage_on() -> list[Any]:
    config = AgentConfig.model_validate(
        {
            "procedures": [
                {
                    "name": "p",
                    "description": "d",
                    "steps": [{"id": "s", "kind": "end", "text": "t", "start": True}],
                }
            ],
            "stt": {"failover": [{"provider": "openai"}]},
            "tts": {"failover": [{"provider": "openai"}]},
            "llm": {"failover": [{"provider": "openai"}]},
        }
    )
    auto_kb = {"knowledge_base_id": str(uuid.uuid4()), "mode": "auto"}
    return create_pipeline(
        config=config,
        transport=MagicMock(),
        call_context=_context(records=False),
        pipecat_settings=SimpleNamespace(),
        credentials=_CREDS,
        knowledge_base_attachments=[auto_kb],
    ).processors


@pytest.mark.asyncio
async def test_every_turncall_processor_is_built_somewhere() -> None:
    """A processor neither the S2S builds above nor a cascade build with every
    stage on reaches is one this file would never drive: turn it on in
    `_every_stage_on` (or `_cascade_every_stage_on`, if it is cascade's)."""
    built = {
        type(p)
        for provider in S2S_PROVIDERS
        for avatar in _AVATARS
        for records in (False, True)
        for p in _build(_every_stage_on(provider, avatar), records=records, avatar=True)
    } | {type(p) for p in _walk(_cascade_every_stage_on())}

    missing = sorted(k.__name__ for k in _turncall_processors() - built)
    assert missing == []
