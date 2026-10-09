"""Ambience reaches every voice transport, never an eval, and never ends a
call (#169, #173, ADR-0021).

Every failure here is inaudible to whoever configured it — a transport with no
mixer, a mixer at the wrong rate, a sound from someone else's project — so the
assertions are structural where a happy path per transport would miss the
fourth transport someone adds later.
"""

import ast
import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

import turncall
from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.domain.models import AgentConfig
from turncall.orchestrator import transport_factory as tf
from turncall.orchestrator.ambience import build_ambience_mixer

pytestmark = pytest.mark.unit

VOICE_FACTORIES = {
    "create_carrier_transport": lambda mixer: tf.create_carrier_transport(
        "twilio", MagicMock(), tf.CarrierStream("MZ1"), mixer=mixer
    ),
    "create_webrtc_transport": lambda mixer: tf.create_webrtc_transport(mixer=mixer)[0],
    "create_whatsapp_transport": lambda mixer: tf.create_whatsapp_transport(
        MagicMock(), mixer=mixer
    ),
}


class TestConfigSchema:
    def test_absent_means_off(self) -> None:
        assert AgentConfigSchema().ambience is None
        assert AgentConfig().ambience is None

    def test_volume_defaults_to_0_3(self) -> None:
        cfg = AgentConfigSchema(ambience={"sound": str(uuid.uuid4())})
        assert cfg.ambience.volume == 0.3

    @pytest.mark.parametrize("volume", [-0.1, 1.01])
    def test_volume_is_bounded(self, volume) -> None:
        with pytest.raises(ValidationError):
            AgentConfigSchema(ambience={"sound": str(uuid.uuid4()), "volume": volume})

    def test_a_sound_is_required_when_the_block_is_present(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfigSchema(ambience={"volume": 0.5})

    def test_unknown_keys_are_refused(self) -> None:
        """No `enabled`: the id's absence already says off."""
        with pytest.raises(ValidationError):
            AgentConfigSchema(ambience={"sound": str(uuid.uuid4()), "enabled": True})

    def test_it_stores_as_json_and_round_trips(self) -> None:
        sound = uuid.uuid4()
        blob = AgentConfigSchema(ambience={"sound": str(sound)}).model_dump()
        assert blob["ambience"] == {"sound": str(sound), "volume": 0.3}
        assert AgentConfig.model_validate(blob).ambience.sound == sound


class TestAnUnusableInlineBlockIsOffNotFatal:
    """An inline config is validated by the domain model only, so its ambience
    block is untrusted: a bad one must cost the room, never the call."""

    @pytest.mark.parametrize(
        "block",
        [{"sound": "not-a-uuid"}, {"sound": str(uuid.uuid4()), "volume": 50}],
        ids=["bad-id", "volume-out-of-range"],
    )
    def test_it_parses_with_ambience_off(self, block) -> None:
        cfg = AgentConfig.model_validate({"system_prompt": "hi", "ambience": block})
        assert cfg.ambience is None
        assert cfg.system_prompt == "hi"


class TestTransports:
    @pytest.mark.parametrize("name", sorted(VOICE_FACTORIES))
    def test_every_voice_transport_carries_the_mixer_it_is_given(self, name) -> None:
        from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer

        mixer = SoundfileMixer(sound_files={}, default_sound="ambience")
        assert VOICE_FACTORIES[name](mixer)._params.audio_out_mixer is mixer

    @pytest.mark.parametrize("name", sorted(VOICE_FACTORIES))
    def test_no_ambience_means_no_mixer(self, name) -> None:
        """A mixer makes the output continuous; without ambience that path
        must never be engaged."""
        assert VOICE_FACTORIES[name](None)._params.audio_out_mixer is None

    def test_every_voice_factory_is_covered(self) -> None:
        """A fourth transport added later has to come through here."""
        factories = {
            n
            for n, f in vars(tf).items()
            if n.startswith("create_") and n.endswith("_transport") and callable(f)
        }
        assert factories - {"create_eval_transport"} == set(VOICE_FACTORIES)

    def test_the_eval_transport_cannot_take_one(self) -> None:
        assert "mixer" not in inspect.signature(tf.create_eval_transport).parameters

    def test_every_voice_transport_call_site_passes_a_mixer(self) -> None:
        """The factory taking a mixer is half of it; each entry point handing
        one over is the other, and forgetting it is silent."""
        src = Path(turncall.__file__).parent
        missing = []
        for path in src.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) in VOICE_FACTORIES
                    and "mixer" not in {k.arg for k in node.keywords}
                ):
                    missing.append(f"{path.name}:{node.lineno}")
        assert not missing, f"voice transports built without a mixer: {missing}"


def _session_factory(row):
    session = AsyncMock()

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    get = AsyncMock(return_value=row)
    return (lambda: _Ctx()), get


def _settings(tmp_path):
    return SimpleNamespace(
        storage=SimpleNamespace(
            ambience_cache_dir=str(tmp_path / "cache"),
            backend="local",
            local_path=str(tmp_path / "store"),
            s3_bucket="",
            aws_region="us-east-1",
        )
    )


@pytest.mark.asyncio
class TestBuildAmbienceMixer:
    SOUND = uuid.uuid4()
    CALL_PROJECT = uuid.uuid4()

    def _config(self, **ambience) -> AgentConfig:
        return AgentConfig.model_validate(
            {"ambience": {"sound": str(self.SOUND), **ambience}}
        )

    async def _build(self, config, row, tmp_path, download=b"mp3"):
        factory, get = _session_factory(row)
        storage = AsyncMock()
        storage.download.return_value = download
        with (
            patch("turncall.orchestrator.ambience.ambience_repo.get_by_id", get),
            patch(
                "turncall.orchestrator.ambience.create_storage_adapter",
                return_value=storage,
            ),
        ):
            mixer = await build_ambience_mixer(
                config,
                project_id=self.CALL_PROJECT,
                sample_rate=8000,
                session_factory=factory,
                settings=_settings(tmp_path),
            )
        return mixer, get, storage

    def _row(self):
        return SimpleNamespace(renditions={"8000": "k/8000.mp3", "16000": "k/16.mp3"})

    async def test_no_block_touches_nothing(self, tmp_path) -> None:
        mixer, get, _ = await self._build(AgentConfig(), None, tmp_path)
        assert mixer is None
        get.assert_not_awaited()

    async def test_a_sound_in_the_calls_project_plays_at_its_volume(
        self, tmp_path
    ) -> None:
        mixer, _, storage = await self._build(
            self._config(volume=0.2), self._row(), tmp_path
        )
        assert mixer is not None
        assert mixer._volume == 0.2
        cached = Path(mixer._sound_files["ambience"])
        assert cached.read_bytes() == b"mp3"
        assert cached.name == "8000.mp3"
        storage.download.assert_awaited_once_with("k/8000.mp3")

    async def test_a_cached_rendition_is_not_fetched_again(self, tmp_path) -> None:
        await self._build(self._config(), self._row(), tmp_path)
        _, _, storage = await self._build(self._config(), self._row(), tmp_path)
        storage.download.assert_not_awaited()

    async def test_it_resolves_against_the_calls_project_only(self, tmp_path) -> None:
        """#173: an inline config is untrusted input. Whatever it claims about
        its own project, the lookup is scoped to the call's."""
        inline = AgentConfig.model_validate(
            {
                "ambience": {"sound": str(self.SOUND), "project_id": str(uuid.uuid4())},
                "project_id": str(uuid.uuid4()),
            }
        )
        _, get, _ = await self._build(inline, None, tmp_path)
        assert get.await_args.kwargs["project_id"] == self.CALL_PROJECT

    async def test_another_projects_sound_warns_and_plays_nothing(
        self, tmp_path
    ) -> None:
        from loguru import logger

        seen: list[str] = []
        handler = logger.add(seen.append, level="WARNING")
        try:
            mixer, _, storage = await self._build(self._config(), None, tmp_path)
        finally:
            logger.remove(handler)
        assert mixer is None
        storage.download.assert_not_awaited()
        assert any(str(self.SOUND) in m for m in seen)

    async def test_a_missing_rendition_plays_nothing(self, tmp_path) -> None:
        row = SimpleNamespace(renditions={"16000": "k/16.mp3"})
        mixer, _, _ = await self._build(self._config(), row, tmp_path)
        assert mixer is None

    async def test_a_hung_fetch_gives_up(self, tmp_path) -> None:
        import asyncio

        async def _hang(key):
            await asyncio.sleep(10)

        factory, get = _session_factory(self._row())
        storage = AsyncMock()
        storage.download.side_effect = _hang
        with (
            patch("turncall.orchestrator.ambience.ambience_repo.get_by_id", get),
            patch(
                "turncall.orchestrator.ambience.create_storage_adapter",
                return_value=storage,
            ),
            patch("turncall.orchestrator.ambience.FETCH_TIMEOUT_SECONDS", 0.01),
        ):
            mixer = await build_ambience_mixer(
                self._config(),
                project_id=self.CALL_PROJECT,
                sample_rate=8000,
                session_factory=factory,
                settings=_settings(tmp_path),
            )
        assert mixer is None

    async def test_a_failed_fetch_never_raises(self, tmp_path) -> None:
        factory, get = _session_factory(self._row())
        storage = AsyncMock()
        storage.download.side_effect = RuntimeError("s3 down")
        with (
            patch("turncall.orchestrator.ambience.ambience_repo.get_by_id", get),
            patch(
                "turncall.orchestrator.ambience.create_storage_adapter",
                return_value=storage,
            ),
        ):
            mixer = await build_ambience_mixer(
                self._config(),
                project_id=self.CALL_PROJECT,
                sample_rate=8000,
                session_factory=factory,
                settings=_settings(tmp_path),
            )
        assert mixer is None


class TestAgentSave:
    def _auth(self, app, pid):
        from turncall.api.deps import get_session
        from turncall.auth.context import AuthContext
        from turncall.auth.dependencies import resolve_auth_context
        from turncall.domain.enums import ProjectRole

        ctx = AuthContext(
            project_id=pid, api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
        )
        app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
        app.app.dependency_overrides[get_session] = lambda: AsyncMock()

    def test_a_sound_not_in_the_project_is_422(self, app) -> None:
        pid = uuid.uuid4()
        self._auth(app, pid)
        get = AsyncMock(return_value=None)
        with (
            patch("turncall.storage.repositories.ambience_repo.get_by_id", get),
            patch(
                "turncall.storage.repositories.agent_repo.create_agent", AsyncMock()
            ) as create,
        ):
            r = app.post(
                "/v1/agents",
                headers={"Authorization": "Bearer x"},
                json={
                    "name": "a",
                    "config": {"ambience": {"sound": str(uuid.uuid4())}},
                },
            )
        assert r.status_code == 422
        assert "ambience.sound" in r.json()["error"]
        assert get.await_args.kwargs["project_id"] == pid
        create.assert_not_awaited()
