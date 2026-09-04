"""Tests for auth context."""

import uuid

import pytest

from turncall.auth.context import AuthContext
from turncall.domain.enums import ProjectRole


@pytest.mark.unit
class TestAuthContext:
    def test_admin_has_all_roles(self) -> None:
        ctx = AuthContext(
            project_id=uuid.uuid4(),
            api_key_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
        )
        assert ctx.has_role(ProjectRole.ADMIN)
        assert ctx.can_write()
        assert ctx.can_admin()

    def test_developer_can_write_not_admin(self) -> None:
        ctx = AuthContext(
            project_id=uuid.uuid4(),
            api_key_id=uuid.uuid4(),
            role=ProjectRole.DEVELOPER,
        )
        assert ctx.has_role(ProjectRole.DEVELOPER)
        assert ctx.can_write()
        assert not ctx.can_admin()

    def test_viewer_cannot_write_or_admin(self) -> None:
        ctx = AuthContext(
            project_id=uuid.uuid4(),
            api_key_id=uuid.uuid4(),
            role=ProjectRole.VIEWER,
        )
        assert ctx.has_role(ProjectRole.VIEWER)
        assert not ctx.can_write()
        assert not ctx.can_admin()

    def test_context_is_frozen(self) -> None:
        ctx = AuthContext(
            project_id=uuid.uuid4(),
            api_key_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
        )
        with pytest.raises(Exception):
            ctx.role = ProjectRole.VIEWER  # type: ignore[misc]

    def test_environment_is_optional(self) -> None:
        ctx = AuthContext(
            project_id=uuid.uuid4(),
            api_key_id=uuid.uuid4(),
            role=ProjectRole.DEVELOPER,
        )
        assert ctx.environment is None

    def test_environment_can_be_set(self) -> None:
        ctx = AuthContext(
            project_id=uuid.uuid4(),
            api_key_id=uuid.uuid4(),
            role=ProjectRole.DEVELOPER,
            environment="production",
        )
        assert ctx.environment == "production"
