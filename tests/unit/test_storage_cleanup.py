"""Deleting a row deletes the object it named (#168).

Document delete and knowledge-base delete used to drop the rows and leave every
uploaded file in object storage; the project sweep missed every recording that
arrived through Twilio's callback, because it assumed the call recorder's key.
Repositories and storage are patched: the rules under test are the endpoints'.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from turncall.api.deps import get_session
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.enums import ProjectRole
from turncall.services import project_deletion as pd
from turncall.services.recording_storage import recording_storage_key

pytestmark = pytest.mark.unit

H = {"Authorization": "Bearer x"}
REPO = "turncall.storage.repositories.knowledge_repo"


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
    with patch("turncall.api.v1.knowledge._storage", return_value=fake):
        yield fake


def _kb(project_id):
    return SimpleNamespace(id=uuid.uuid4(), project_id=project_id)


class TestDocumentDelete:
    def _delete(self, app, kb, doc, *, key_in_use=False):
        with (
            patch(f"{REPO}.get_knowledge_base_by_id", AsyncMock(return_value=kb)),
            patch(f"{REPO}.get_document_by_id", AsyncMock(return_value=doc)),
            patch(f"{REPO}.delete_document", AsyncMock()) as delete,
            patch(f"{REPO}.storage_key_in_use", AsyncMock(return_value=key_in_use)),
        ):
            r = app.delete(f"/v1/knowledge-bases/{kb.id}/documents/{doc.id}", headers=H)
        return r, delete

    def test_deletes_the_stored_file(self, app, project_id, storage):
        kb = _kb(project_id)
        doc = SimpleNamespace(id=uuid.uuid4(), storage_key=f"kb/{kb.id}/docs/faq.pdf")
        r, delete = self._delete(app, kb, doc)
        assert r.status_code == 204
        delete.assert_awaited_once()
        storage.delete.assert_awaited_once_with(doc.storage_key)

    def test_keeps_a_file_another_document_still_names(self, app, project_id, storage):
        # Rows from before per-upload keys: one filename, one shared key.
        kb = _kb(project_id)
        doc = SimpleNamespace(id=uuid.uuid4(), storage_key=f"kb/{kb.id}/docs/faq.pdf")
        r, _ = self._delete(app, kb, doc, key_in_use=True)
        assert r.status_code == 204
        storage.delete.assert_not_called()

    def test_a_storage_failure_does_not_fail_the_delete(self, app, project_id, storage):
        storage.delete.side_effect = RuntimeError("s3 down")
        kb = _kb(project_id)
        doc = SimpleNamespace(id=uuid.uuid4(), storage_key=f"kb/{kb.id}/docs/faq.pdf")
        r, delete = self._delete(app, kb, doc)
        assert r.status_code == 204
        delete.assert_awaited_once()


class TestKnowledgeBaseDelete:
    def test_deletes_every_document_file(self, app, project_id, storage):
        kb = _kb(project_id)
        keys = [f"kb/{kb.id}/docs/a.pdf", f"kb/{kb.id}/docs/b.txt"]
        with (
            patch(f"{REPO}.get_knowledge_base_by_id", AsyncMock(return_value=kb)),
            patch(f"{REPO}.count_agents_using_kb", AsyncMock(return_value=0)),
            patch(f"{REPO}.document_storage_keys", AsyncMock(return_value=keys)),
            patch(f"{REPO}.delete_knowledge_base", AsyncMock()) as delete,
        ):
            r = app.delete(f"/v1/knowledge-bases/{kb.id}", headers=H)
        assert r.status_code == 204
        delete.assert_awaited_once()
        assert [c.args[0] for c in storage.delete.await_args_list] == keys

    def test_refused_while_attached_touches_no_storage(self, app, project_id, storage):
        kb = _kb(project_id)
        with (
            patch(f"{REPO}.get_knowledge_base_by_id", AsyncMock(return_value=kb)),
            patch(f"{REPO}.count_agents_using_kb", AsyncMock(return_value=1)),
        ):
            r = app.delete(f"/v1/knowledge-bases/{kb.id}", headers=H)
        assert r.status_code == 409
        storage.delete.assert_not_called()


class TestRecordingKey:
    @pytest.mark.parametrize(
        ("url", "key"),
        [
            # call recorder, S3 and local
            ("s3://call-recordings/recordings/abc.wav", "recordings/abc.wav"),
            ("./storage/recordings/abc.wav", "recordings/abc.wav"),
            # Twilio's recording callback nests by call
            ("s3://b/recordings/abc/RE1.wav", "recordings/abc/RE1.wav"),
            ("/data/storage/recordings/abc/RE1.wav", "recordings/abc/RE1.wav"),
        ],
    )
    def test_parses_both_backends_and_both_writers(self, url, key):
        assert recording_storage_key(url) == key

    @pytest.mark.parametrize("url", [None, "", "https://api.twilio.com/RE1"])
    def test_none_when_nothing_is_stored(self, url):
        assert recording_storage_key(url) is None


@pytest.mark.asyncio
async def test_sweep_deletes_twilio_recordings_by_their_real_key():
    pid = uuid.uuid4()
    session = AsyncMock()
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    docs = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: ["kb/k/a"]))
    recs = SimpleNamespace(
        all=lambda: [
            (c1, f"s3://b/recordings/{c1}.wav"),
            # Twilio's callback overwrote recording_url: the recorder's WAV
            # is still stored under the call id and must go too.
            (c2, f"s3://b/recordings/{c2}/RE9.wav"),
        ]
    )
    session.execute.side_effect = [docs, recs]
    storage = AsyncMock()
    with (
        patch.object(pd, "create_storage_adapter", return_value=storage),
        patch.object(
            pd.ambience_repo,
            "rendition_keys_for_project",
            AsyncMock(return_value=["ambience/x/8000.mp3"]),
        ),
    ):
        storage_settings = SimpleNamespace(
            backend="local", local_path="", s3_bucket="", aws_region=""
        )
        await pd._sweep_storage(session, SimpleNamespace(storage=storage_settings), pid)
    assert {c.args[0] for c in storage.delete.await_args_list} == {
        "kb/k/a",
        f"recordings/{c1}.wav",
        f"recordings/{c2}.wav",
        f"recordings/{c2}/RE9.wav",
        "ambience/x/8000.mp3",
    }


@pytest.mark.asyncio
async def test_two_uploads_of_one_filename_get_two_objects():
    from turncall.services import document_ingestion as di

    storage, kb = AsyncMock(), uuid.uuid4()
    with patch.object(di.knowledge_repo, "create_document", AsyncMock()):
        for _ in range(2):
            await di.create_pending_document(
                AsyncMock(),
                storage,
                knowledge_base_id=kb,
                filename="faq.pdf",
                content_type="application/pdf",
                data=b"x",
            )
    a, b = (c.args[0] for c in storage.upload.await_args_list)
    assert a != b
    assert a.startswith(f"kb/{kb}/docs/") and a.endswith("/faq.pdf")
