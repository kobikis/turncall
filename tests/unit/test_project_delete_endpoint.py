"""DELETE /v1/projects/{id}: self-delete only, admin only, 404 if gone,
502 if the Twilio unbind fails (ADR-0011)."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from turncall.api.deps import get_session
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.enums import ProjectRole


def _auth(app, role, project_id):
    ctx = AuthContext(project_id=project_id, api_key_id=uuid.uuid4(), role=role)
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()


@pytest.mark.unit
def test_viewer_and_developer_blocked(app):
    pid = uuid.uuid4()
    for role in (ProjectRole.VIEWER, ProjectRole.DEVELOPER):
        _auth(app, role, pid)
        r = app.delete(f"/v1/projects/{pid}", headers={"Authorization": "Bearer x"})
        assert r.status_code == 403, f"{role} -> {r.status_code}"


@pytest.mark.unit
def test_cannot_delete_other_project(app):
    _auth(app, ProjectRole.ADMIN, uuid.uuid4())
    other = uuid.uuid4()
    r = app.delete(f"/v1/projects/{other}", headers={"Authorization": "Bearer x"})
    assert r.status_code == 403  # a project can only delete itself


@pytest.mark.unit
def test_admin_self_delete_runs_deletion(app):
    pid = uuid.uuid4()
    _auth(app, ProjectRole.ADMIN, pid)
    with (
        patch(
            "turncall.storage.repositories.project_repo.get_project_by_id",
            new=AsyncMock(return_value=object()),  # project exists
        ),
        patch("turncall.services.project_deletion.delete_project", new=AsyncMock()) as run,
    ):
        r = app.delete(f"/v1/projects/{pid}", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True
    run.assert_awaited_once()


@pytest.mark.unit
def test_404_when_already_deleted(app):
    pid = uuid.uuid4()
    _auth(app, ProjectRole.ADMIN, pid)
    with patch(
        "turncall.storage.repositories.project_repo.get_project_by_id",
        new=AsyncMock(return_value=None),  # already soft-deleted / gone
    ):
        r = app.delete(f"/v1/projects/{pid}", headers={"Authorization": "Bearer x"})
    assert r.status_code == 404


@pytest.mark.unit
def test_502_when_unbind_fails(app):
    pid = uuid.uuid4()
    _auth(app, ProjectRole.ADMIN, pid)
    with (
        patch(
            "turncall.storage.repositories.project_repo.get_project_by_id",
            new=AsyncMock(return_value=object()),
        ),
        patch(
            "turncall.services.project_deletion.delete_project",
            new=AsyncMock(side_effect=RuntimeError("twilio down")),
        ),
    ):
        r = app.delete(f"/v1/projects/{pid}", headers={"Authorization": "Bearer x"})
    assert r.status_code == 502


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auth_rejects_soft_deleted_projects_key():
    """The enforcement point: a valid, non-revoked key whose project is
    soft-deleted no longer authenticates (ADR-0011)."""
    from unittest.mock import MagicMock

    from turncall.api.errors import UnauthorizedError
    from turncall.auth import dependencies as dep

    key_row = MagicMock(
        project_id=uuid.uuid4(),
        id=uuid.uuid4(),
        role="admin",
        revoked_at=None,
        environment="production",
        key_prefix="tc_abc",
    )
    with (
        patch.object(dep.api_key_repo, "get_api_key_by_hash", new=AsyncMock(return_value=key_row)),
        patch.object(dep.project_repo, "get_project_by_id", new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(UnauthorizedError):
            await dep.resolve_auth_context(AsyncMock(), authorization="Bearer tc_live")
