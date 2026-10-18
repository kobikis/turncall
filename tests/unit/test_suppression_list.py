"""The suppression list's CRUD, scoped to the API key's project (#227).

That the dial refuses a listed number is pinned in test_outbound_dial.py;
the repository's SQL against Postgres in tests/integration.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from turncall.api.deps import get_session
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.enums import ProjectRole

pytestmark = pytest.mark.unit

H = {"Authorization": "Bearer x"}
REPO = "turncall.storage.repositories.suppression_repo"
URL = "/v1/suppression-list"


def _entry(project_id, number="+15550000002"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project_id,
        e164_number=number,
        created_at=datetime(2026, 10, 2, tzinfo=UTC),
    )


@pytest.fixture
def ctx(app):
    ctx = AuthContext(
        project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
    )
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()
    return ctx


def test_add_suppresses_in_the_keys_project(app, ctx) -> None:
    entry = _entry(ctx.project_id)
    with patch(f"{REPO}.add", AsyncMock(return_value=entry)) as add:
        r = app.post(URL, headers=H, json={"number": "+15550000002"})
    assert r.status_code == 201, r.text
    assert r.json()["data"] == {
        "id": str(entry.id),
        "number": "+15550000002",
        "created_at": "2026-10-02T00:00:00Z",
    }
    assert add.await_args.kwargs == {
        "project_id": ctx.project_id,
        "number": "+15550000002",
    }


@pytest.mark.parametrize("number", ["5550000002", "+0123", "+1 555 000 0002", ""])
def test_add_refuses_a_number_that_is_not_e164(app, ctx, number) -> None:
    with patch(f"{REPO}.add", AsyncMock()) as add:
        r = app.post(URL, headers=H, json={"number": number})
    assert r.status_code == 422
    add.assert_not_awaited()


def test_list_pages_the_keys_project(app, ctx) -> None:
    entry = _entry(ctx.project_id)
    with patch(
        f"{REPO}.list_for_project", AsyncMock(return_value=([entry], 7))
    ) as lister:
        r = app.get(f"{URL}?page=2&limit=1&number=%2B15550000002", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["total"], body["page"], body["limit"]) == (7, 2, 1)
    assert [e["id"] for e in body["data"]] == [str(entry.id)]
    assert lister.await_args.args[1] == ctx.project_id
    assert lister.await_args.kwargs == {
        "limit": 1,
        "offset": 1,
        "number": "+15550000002",
    }


def test_delete_is_scoped_to_the_keys_project(app, ctx) -> None:
    entry_id = uuid.uuid4()
    with patch(f"{REPO}.remove", AsyncMock(return_value=True)) as remove:
        r = app.delete(f"{URL}/{entry_id}", headers=H)
    assert r.status_code == 200, r.text
    assert remove.await_args.kwargs == {
        "project_id": ctx.project_id,
        "entry_id": entry_id,
    }


def test_delete_of_an_entry_not_in_the_project_is_404(app, ctx) -> None:
    with patch(f"{REPO}.remove", AsyncMock(return_value=False)):
        r = app.delete(f"{URL}/{uuid.uuid4()}", headers=H)
    assert r.status_code == 404


@pytest.mark.parametrize(
    "method,path", [("post", URL), ("delete", f"{URL}/{uuid.UUID(int=1)}")]
)
def test_a_viewer_cannot_change_the_list(app, method, path) -> None:
    ctx = AuthContext(
        project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=ProjectRole.VIEWER
    )
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()
    r = app.request(method.upper(), path, headers=H)
    assert r.status_code == 403
