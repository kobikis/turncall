"""The ambience-sounds resource rules (#167, ADR-0021).

The 409-while-attached rule exists twice in this codebase and was untested both
times; this is where it gets coverage. Repositories and storage are patched: the
rules under test are the endpoint's, not Postgres's.
"""

import io
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
import soundfile as sf

from turncall.api.deps import get_session
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.enums import ProjectRole
from turncall.orchestrator.transport_factory import VOICE_OUTPUT_RATES

pytestmark = pytest.mark.unit

H = {"Authorization": "Bearer x"}
REPO = "turncall.storage.repositories.ambience_repo"


@pytest.fixture
def project_id(app):
    pid = uuid.uuid4()
    ctx = AuthContext(project_id=pid, api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN)
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()
    return pid


@pytest.fixture
def storage():
    fake = AsyncMock()
    fake.get_presigned_url.side_effect = lambda key: f"https://signed/{key}"
    with patch("turncall.api.v1.ambience._storage", return_value=fake):
        yield fake


def _row(project_id, **over):
    sid = over.pop("id", uuid.uuid4())
    return SimpleNamespace(
        id=sid,
        project_id=project_id,
        name="dining-room.wav",
        duration_ms=1000,
        renditions={
            str(r): f"ambience/{project_id}/{sid}/{r}.mp3" for r in (8000, 16000)
        },
        **over,
    )


def _wav(seconds: float = 1.0) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(16000 * seconds)), 16000, format="WAV")
    return buf.getvalue()


class TestUpload:
    def test_stores_one_rendition_per_voice_rate(self, app, project_id, storage):
        create = AsyncMock(
            side_effect=lambda session, **kw: _row(project_id, **{"id": kw["sound_id"]})
        )
        with patch(f"{REPO}.create_sound", create):
            r = app.post(
                "/v1/ambience-sounds",
                headers=H,
                files={"file": ("room.wav", _wav(), "audio/wav")},
            )
        assert r.status_code == 201, r.text
        stored = {c.args[0] for c in storage.upload.await_args_list}
        assert {k.rsplit("/", 1)[1] for k in stored} == {
            f"{rate}.mp3" for rate in VOICE_OUTPUT_RATES
        }
        assert all(k.startswith(f"ambience/{project_id}/") for k in stored)

    def test_unreadable_is_415_naming_the_fix(self, app, project_id, storage):
        r = app.post(
            "/v1/ambience-sounds",
            headers=H,
            files={
                "file": ("memo.m4a", b"\x00\x00\x00\x20ftypM4A " * 50, "audio/mpeg")
            },
        )
        assert r.status_code == 415
        assert "ffmpeg" in r.json()["error"]
        storage.upload.assert_not_awaited()

    def test_oversized_body_is_409(self, app, project_id, storage):
        with patch("turncall.api.v1.ambience.MAX_UPLOAD_SIZE", 100):
            r = app.post(
                "/v1/ambience-sounds",
                headers=H,
                files={"file": ("room.wav", _wav(), "audio/wav")},
            )
        assert r.status_code == 409
        assert "too large" in r.json()["error"]

    def test_a_failed_row_write_removes_the_uploaded_objects(
        self, app, project_id, storage
    ):
        with patch(f"{REPO}.create_sound", AsyncMock(side_effect=RuntimeError("db"))):
            r = app.post(
                "/v1/ambience-sounds",
                headers=H,
                files={"file": ("room.wav", _wav(), "audio/wav")},
            )
        assert r.status_code == 500
        assert storage.delete.await_count == len(VOICE_OUTPUT_RATES)


class TestRead:
    def test_returns_a_playable_url_per_rendition(self, app, project_id, storage):
        row = _row(project_id)
        with patch(f"{REPO}.get_by_id", AsyncMock(return_value=row)):
            r = app.get(f"/v1/ambience-sounds/{row.id}", headers=H)
        assert r.status_code == 200
        urls = r.json()["data"]["rendition_urls"]
        assert set(urls) == {"8000", "16000"}
        assert urls["8000"].startswith("https://signed/")

    def test_another_projects_sound_is_404(self, app, project_id, storage):
        """The repo is always asked with the caller's project; a miss is a 404."""
        get = AsyncMock(return_value=None)
        sid = uuid.uuid4()
        with patch(f"{REPO}.get_by_id", get):
            r = app.get(f"/v1/ambience-sounds/{sid}", headers=H)
        assert r.status_code == 404
        assert get.await_args.kwargs["project_id"] == project_id


class TestDelete:
    def test_refused_while_an_agent_names_it_listing_the_agents(
        self, app, project_id, storage
    ):
        row = _row(project_id)
        agent = SimpleNamespace(id=uuid.uuid4(), name="bookings", version=3)
        with (
            patch(f"{REPO}.get_by_id", AsyncMock(return_value=row)),
            patch(f"{REPO}.agents_referencing", AsyncMock(return_value=[agent])),
            patch(f"{REPO}.delete_sound", AsyncMock()) as delete,
        ):
            r = app.delete(f"/v1/ambience-sounds/{row.id}", headers=H)
        assert r.status_code == 409
        body = r.json()
        assert "bookings v3" in body["error"]
        assert body["details"]["agents"][0]["id"] == str(agent.id)
        delete.assert_not_awaited()
        storage.delete.assert_not_awaited()

    def test_deletes_the_row_and_its_stored_objects(self, app, project_id, storage):
        row = _row(project_id)
        with (
            patch(f"{REPO}.get_by_id", AsyncMock(return_value=row)),
            patch(f"{REPO}.agents_referencing", AsyncMock(return_value=[])),
            patch(f"{REPO}.delete_sound", AsyncMock()) as delete,
        ):
            r = app.delete(f"/v1/ambience-sounds/{row.id}", headers=H)
        assert r.status_code == 200
        delete.assert_awaited_once()
        assert {c.args[0] for c in storage.delete.await_args_list} == set(
            row.renditions.values()
        )
