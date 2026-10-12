"""PATCH /v1/webhooks/{id} changes a subscription's events and keeps its
secret: the receiver, and the tools signed with the same secret, keep
verifying. Delete-and-recreate issued a new secret."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from turncall.api.deps import get_session
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.enums import ProjectRole

pytestmark = pytest.mark.unit

H = {"Authorization": "Bearer x"}


def _session(row):
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    return AsyncMock(execute=AsyncMock(return_value=result))


@pytest.fixture
def ctx(app):
    ctx = AuthContext(
        project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
    )
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    return ctx


def test_events_are_replaced_and_no_secret_is_issued(app, ctx) -> None:
    row = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=ctx.project_id,
        url="https://backend/events",
        events={"events": ["*"]},
        active=True,
        created_at=datetime(2026, 10, 12, tzinfo=UTC),
    )
    session = _session(row)
    app.app.dependency_overrides[get_session] = lambda: session

    r = app.patch(f"/v1/webhooks/{row.id}", headers=H, json={"events": ["*"]})

    assert r.status_code == 200, r.text
    assert r.json()["data"]["events"] == ["*"]
    assert "secret" not in r.json()["data"]
    sql = str(session.execute.await_args.args[0].compile())
    assert "project_id" in sql and "secret" not in sql.split("SET")[1].split("WHERE")[0]


def test_a_webhook_outside_the_project_is_404(app, ctx) -> None:
    app.app.dependency_overrides[get_session] = lambda: _session(None)
    r = app.patch(f"/v1/webhooks/{uuid.uuid4()}", headers=H, json={"events": ["*"]})
    assert r.status_code == 404


def test_an_empty_list_is_refused(app, ctx) -> None:
    app.app.dependency_overrides[get_session] = lambda: _session(None)
    r = app.patch(f"/v1/webhooks/{uuid.uuid4()}", headers=H, json={"events": []})
    assert r.status_code == 422
