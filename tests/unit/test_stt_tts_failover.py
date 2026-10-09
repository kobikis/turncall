"""STT and TTS failover (#223): backup providers for the two speech roles.

The switching rules are the LLM's (#222); what these pin is what "the call
continues" means for each role — the backup hears the speech the failed STT
never transcribed, and speaks the reply the failed TTS never said.
"""

import asyncio

import pytest
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import run_test
from pipecat.utils.errors import ErrorCategory
from pydantic import ValidationError

from tests.conftest import creds
from turncall.api.v1.schemas.agents import (
    AgentConfigSchema,
    STTConfigSchema,
    TTSConfigSchema,
)
from turncall.domain.models import AgentConfig
from turncall.orchestrator.failover import FailoverSTT, FailoverTTS
from turncall.orchestrator.pipeline_factory import (
    _create_stt_stage,
    _create_tts_stage,
)

_AUDIO = b"\x00\x01" * 160


def _audio() -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=_AUDIO, sample_rate=8000, num_channels=1)


class _FakeSTT(FrameProcessor):
    """A segmented STT: buffers audio between VAD brackets and transcribes the
    segment at the stop — or, with `fail`, is refused there."""

    def __init__(self, fail: ErrorCategory | None = None) -> None:
        super().__init__()
        self.fail = fail
        self.speaking = False
        self.segment = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self.speaking, self.segment = True, 0
        elif isinstance(frame, InputAudioRawFrame) and self.speaking:
            self.segment += 1
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.speaking = False
            if self.fail is not None:
                await self.push_error("stt refused", category=self.fail)
            else:
                await self.push_frame(
                    TranscriptionFrame(
                        f"{self.segment} chunks", "", "t", finalized=True
                    )
                )
        await self.push_frame(frame, direction)


class _FakeTTS(FrameProcessor):
    """Speaks a reply (or a TTSSpeakFrame) as one TTSTextFrame — or, with
    `fail`, is refused on the first sentence it is asked to say."""

    def __init__(self, fail: ErrorCategory | None = None) -> None:
        super().__init__()
        self.fail = fail
        self.text: list[str] = []
        self.spoken: list[str] = []

    async def _speak(self, text: str) -> None:
        if self.fail is not None:
            await self.push_error("tts refused", category=self.fail)
            return
        self.spoken.append(text)
        await self.push_frame(TTSTextFrame(text, aggregated_by="sentence"))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            self.text.append(frame.text)
            if frame.text.endswith("."):
                # A sentence is ready: the first thing a TTS is asked to say.
                await self._speak("".join(self.text))
                self.text = []
            return
        if isinstance(frame, TTSSpeakFrame):
            await self._speak(frame.text)
            return
        if isinstance(frame, LLMFullResponseEndFrame) and self.text:
            await self._speak("".join(self.text))
            self.text = []
        await self.push_frame(frame, direction)


def _watch(group: object) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []

    @group.event_handler("on_failover")  # type: ignore[attr-defined]
    async def _on(_g: object, src: str, dst: str, error: ErrorFrame) -> None:
        seen.append((src, dst, error.error))

    return seen


@pytest.mark.unit
class TestSTT:
    async def test_the_backup_hears_the_sentence_the_primary_was_refused_on(
        self,
    ) -> None:
        backup = _FakeSTT()
        group = FailoverSTT(
            [
                ("openai/gpt-transcribe", _FakeSTT(ErrorCategory.AUTHENTICATION)),
                ("deepgram/nova-3-general", backup),
            ]
        )
        seen = _watch(group)

        down, up = await run_test(
            group,
            frames_to_send=[
                VADUserStartedSpeakingFrame(),
                _audio(),
                _audio(),
                _audio(),
                VADUserStoppedSpeakingFrame(),
            ],
        )

        transcripts = [f.text for f in down if isinstance(f, TranscriptionFrame)]
        assert transcripts == ["3 chunks"]
        assert seen == [
            ("openai/gpt-transcribe", "deepgram/nova-3-general", "stt refused")
        ]
        # The replay is not delivered downstream a second time.
        assert len([f for f in down if isinstance(f, InputAudioRawFrame)]) == 3
        # The switch absorbed the error: nothing upstream ends the call.
        assert not [f for f in up if isinstance(f, ErrorFrame)]

    async def test_nothing_is_replayed_when_everything_said_was_heard(self) -> None:
        primary, backup = _FakeSTT(), _FakeSTT()
        group = FailoverSTT([("a/1", primary), ("b/2", backup)])
        await run_test(
            group,
            frames_to_send=[
                VADUserStartedSpeakingFrame(),
                _audio(),
                VADUserStoppedSpeakingFrame(),
            ],
        )
        # A transcript came out: a failure from here on owes the backup nothing.
        assert group._speech is False and not group._unheard
        assert backup.segment == 0

    async def test_recoverable_error_does_not_switch(self) -> None:
        primary, backup = _FakeSTT(ErrorCategory.RATE_LIMIT), _FakeSTT()
        group = FailoverSTT([("a/1", primary), ("b/2", backup)])

        _down, up = await run_test(
            group,
            frames_to_send=[
                VADUserStartedSpeakingFrame(),
                VADUserStoppedSpeakingFrame(),
            ],
        )

        assert group.strategy.active_service is primary
        assert [f for f in up if isinstance(f, ErrorFrame)]


@pytest.mark.unit
class TestTTS:
    async def test_the_backup_says_the_whole_reply(self) -> None:
        backup = _FakeTTS()
        group = FailoverTTS(
            [
                ("openai/tts-1", _FakeTTS(ErrorCategory.AUTHENTICATION)),
                ("deepgram/aura-2-helena-en", backup),
            ]
        )
        seen = _watch(group)

        down, up = await run_test(
            group,
            frames_to_send=[
                LLMFullResponseStartFrame(),
                LLMTextFrame("Your table is booked."),
                LLMTextFrame(" See you at"),
                LLMTextFrame(" eight."),
                LLMFullResponseEndFrame(),
            ],
        )

        assert backup.spoken == ["Your table is booked.", " See you at eight."]
        assert seen == [("openai/tts-1", "deepgram/aura-2-helena-en", "tts refused")]
        # One bracket downstream: the replay's frames are the originals.
        assert len([f for f in down if isinstance(f, LLMFullResponseEndFrame)]) == 1
        assert not [f for f in up if isinstance(f, ErrorFrame)]

    async def test_a_refused_greeting_is_spoken_by_the_backup(self) -> None:
        backup = _FakeTTS()
        group = FailoverTTS(
            [("a/1", _FakeTTS(ErrorCategory.AUTHENTICATION)), ("b/2", backup)]
        )
        await run_test(group, frames_to_send=[TTSSpeakFrame("Welcome to Nonna's.")])
        assert backup.spoken == ["Welcome to Nonna's."]

    async def test_recoverable_error_does_not_switch(self) -> None:
        primary, backup = _FakeTTS(ErrorCategory.RATE_LIMIT), _FakeTTS()
        group = FailoverTTS([("a/1", primary), ("b/2", backup)])

        _down, up = await run_test(group, frames_to_send=[TTSSpeakFrame("Hi.")])

        assert group.strategy.active_service is primary
        assert backup.spoken == []
        assert [f for f in up if isinstance(f, ErrorFrame)]


@pytest.mark.unit
@pytest.mark.parametrize("group_cls", [FailoverSTT, FailoverTTS])
async def test_no_switching_back(group_cls: type) -> None:
    """A failed member is held down even when pipecat would revive it on a
    settings change — so the caller never hears the voice change twice."""
    primary = _FakeTTS() if group_cls is FailoverTTS else _FakeSTT()
    backup = _FakeTTS() if group_cls is FailoverTTS else _FakeSTT()
    group = group_cls([("a/1", primary), ("b/2", backup)])
    await run_test(group, frames_to_send=[])
    await primary.push_error("dead", category=ErrorCategory.AUTHENTICATION)
    await group.push_frame(
        ErrorFrame("dead", processor=primary, category=ErrorCategory.AUTHENTICATION),
        FrameDirection.UPSTREAM,
    )
    assert group.strategy.active_service is backup

    await primary.set_usable(True)  # what a changed setting does
    await asyncio.sleep(0.01)  # pipecat runs event handlers as tasks

    assert not primary.is_usable


@pytest.mark.unit
class TestValidation:
    def test_s2s_agent_with_speech_failover_is_refused(self) -> None:
        for block in ("stt", "tts"):
            entry = (
                {"provider": "openai", "voice": "alloy"}
                if block == "tts"
                else {"provider": "openai"}
            )
            with pytest.raises(ValidationError, match="realtime model"):
                AgentConfigSchema(pipeline_mode="s2s", **{block: {"failover": [entry]}})

    def test_entries_are_validated_like_the_role_itself(self) -> None:
        with pytest.raises(ValidationError, match="Unsupported STT provider"):
            STTConfigSchema(failover=[{"provider": "nope"}])
        with pytest.raises(ValidationError, match="Invalid OpenAI voice"):
            TTSConfigSchema(failover=[{"provider": "openai", "voice": "rachel"}])

    def test_entry_cannot_nest_its_own_failover(self) -> None:
        with pytest.raises(ValidationError, match="nest"):
            STTConfigSchema(
                failover=[
                    {"provider": "openai", "failover": [{"provider": "deepgram"}]}
                ]
            )

    def test_flux_cannot_share_a_group_with_other_models(self) -> None:
        with pytest.raises(ValidationError, match="Flux"):
            STTConfigSchema(
                failover=[{"provider": "deepgram", "model": "flux-general-en"}]
            )
        both_flux = STTConfigSchema(
            model="flux-general-en",
            failover=[{"provider": "deepgram", "model": "flux-general-multi"}],
        )
        assert len(both_flux.failover) == 1

    def test_survives_ingest_to_domain(self) -> None:
        schema = AgentConfigSchema(
            stt={"failover": [{"provider": "openai"}]},
            tts={"failover": [{"provider": "openai", "voice": "alloy"}]},
        )
        config = AgentConfig.model_validate(schema.model_dump())
        assert config.stt.failover[0].provider == "openai"
        assert config.tts.failover[0].voice == "alloy"


@pytest.mark.unit
class TestBuild:
    def test_no_failover_builds_plain_services(self) -> None:
        config = AgentConfig()
        assert not isinstance(
            _create_stt_stage(config, creds(deepgram="dg"), sample_rate=8000),
            FailoverSTT,
        )
        assert not isinstance(
            _create_tts_stage(config, creds(deepgram="dg")), FailoverTTS
        )

    def test_entries_build_on_their_own_terms(self) -> None:
        config = AgentConfig.model_validate(
            {
                "stt": {"failover": [{"provider": "openai"}]},
                "tts": {"failover": [{"provider": "openai", "voice": "alloy"}]},
            }
        )
        keys = creds("sk-test", deepgram="dg")
        stt = _create_stt_stage(config, keys, sample_rate=8000)
        tts = _create_tts_stage(config, keys)
        assert isinstance(stt, FailoverSTT) and isinstance(tts, FailoverTTS)
        # Each entry resolves its own model; Deepgram's default never leaks.
        assert [stt.label(s) for s in stt.services] == [
            "deepgram/nova-3-general",
            "openai/gpt-transcribe",
        ]
        assert [tts.label(s) for s in tts.services] == [
            "deepgram/aura-2-helena-en",
            "openai/tts-1",
        ]

    def test_assemblyai_entry_resolves_its_model_from_its_own_language(
        self,
    ) -> None:
        from pipecat.services.assemblyai.stt import AssemblyAISTTService

        config = AgentConfig.model_validate(
            {"stt": {"failover": [{"provider": "assemblyai", "language": "es"}]}}
        )
        stt = _create_stt_stage(
            config, creds(deepgram="dg", assemblyai="aai"), sample_rate=8000
        )
        assert isinstance(stt, FailoverSTT)
        assert isinstance(stt.services[1], AssemblyAISTTService)
        # The label is what `provider.failover` names; an English label on a
        # Spanish entry would misreport the model the call switched to.
        assert stt.label(stt.services[1]) == (
            "assemblyai/universal-streaming-multilingual"
        )

    @pytest.mark.parametrize(
        "stt",
        [
            {
                "model": "flux-general-en",
                "failover": [{"provider": "deepgram", "model": "nova-3-general"}],
            },
            {
                "provider": "deepgram",
                "failover": [{"provider": "deepgram", "model": "flux-general-en"}],
            },
            {
                "model": "flux-general-en",
                "failover": [{"provider": "openai"}],
            },
        ],
        ids=["flux-then-nova", "nova-then-flux", "flux-then-openai"],
    )
    def test_a_mixed_flux_group_never_builds(self, stt: dict) -> None:
        """Either direction, and whichever way the config arrived: a stored
        agent is refused at the API (422), but a call-init inline agent never
        passes through it — it reaches the build as a plain domain config."""
        with pytest.raises(ValidationError, match="Flux"):
            STTConfigSchema.model_validate(stt)
        config = AgentConfig.model_validate({"stt": stt})
        with pytest.raises(ValueError, match="Flux"):
            _create_stt_stage(config, creds("sk", deepgram="dg"), sample_rate=8000)

    def test_flux_only_group_builds_flux_members(self) -> None:
        from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService

        config = AgentConfig.model_validate(
            {
                "stt": {
                    "model": "flux-general-en",
                    "failover": [
                        {"provider": "deepgram", "model": "flux-general-multi"}
                    ],
                }
            }
        )
        stt = _create_stt_stage(config, creds(deepgram="dg"), sample_rate=8000)
        assert isinstance(stt, FailoverSTT)
        assert all(isinstance(s, DeepgramFluxSTTService) for s in stt.services)
        assert [stt.label(s) for s in stt.services] == [
            "deepgram/flux-general-en",
            "deepgram/flux-general-multi",
        ]

    def test_missing_backup_keys_are_reported(self) -> None:
        config = AgentConfig.model_validate(
            {
                "stt": {"failover": [{"provider": "cartesia"}]},
                "tts": {"failover": [{"provider": "elevenlabs"}]},
            }
        )
        missing = creds(
            "sk-test", deepgram="dg", cartesia="", elevenlabs=""
        ).missing_keys(config)
        assert {"cartesia", "elevenlabs"} <= set(missing)


@pytest.mark.unit
@pytest.mark.parametrize("role", ["stt", "tts"])
async def test_session_records_the_failover(role: str) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from pipecat.pipeline.pipeline import Pipeline

    from turncall.domain.enums import CallEventType
    from turncall.orchestrator.call_session import CallSession

    cls, fake = (FailoverSTT, _FakeSTT) if role == "stt" else (FailoverTTS, _FakeTTS)
    group = cls([("a/1", fake()), ("b/2", fake())])
    cs = CallSession(
        call_context=MagicMock(is_eval=False),
        transport=MagicMock(),
        pipeline=Pipeline([group]),
    )
    cs._watch_failover()

    with patch.object(cs, "_log_event", AsyncMock()) as log_event:
        await group._call_event_handler(
            "on_failover", "a/1", "b/2", ErrorFrame(error="401 bad key")
        )
        await asyncio.sleep(0.01)  # pipecat runs event handlers as tasks

    assert cs.pipeline_errors == [f"{role} failed over from a/1 to b/2: 401 bad key"]
    log_event.assert_awaited_once_with(
        CallEventType.PROVIDER_FAILOVER,
        {"role": role, "from": "a/1", "to": "b/2", "error": "401 bad key"},
    )
