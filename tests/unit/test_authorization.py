"""Authorization cluster (review findings #4/#7/#8): role hierarchy, config
secret masking, and project-scoped key revocation."""

import uuid
from unittest.mock import AsyncMock

import pytest

from turncall.api.v1.schemas.agents import _sanitize_config
from turncall.domain.enums import ProjectRole


@pytest.mark.unit
class TestRoleRank:
    def test_ordering(self) -> None:
        assert ProjectRole.ADMIN.rank > ProjectRole.DEVELOPER.rank
        assert ProjectRole.DEVELOPER.rank > ProjectRole.VIEWER.rank

    def test_escalation_guard_logic(self) -> None:
        # The endpoint forbids body.role.rank > auth.role.rank.
        assert ProjectRole.ADMIN.rank > ProjectRole.DEVELOPER.rank  # dev cannot mint admin
        assert not ProjectRole.VIEWER.rank > ProjectRole.DEVELOPER.rank  # dev may mint viewer
        assert not ProjectRole.ADMIN.rank > ProjectRole.ADMIN.rank  # admin may mint admin


@pytest.mark.unit
class TestSanitizeConfig:
    def _secretful(self) -> dict:
        return {
            "system_prompt": "hi",
            "llm": {"provider": "openai", "model": "gpt-4o", "api_key": "sk-secret"},
            "server_url": {"url": "https://x", "secret": "sig-secret"},
            "tools": [
                {"name": "book", "webhook_url": "https://x", "webhook_secret": "tool-secret"},
                {"name": "end_call"},  # no secret — untouched
            ],
            "mcp_servers": [
                {"name": "crm", "headers": {"Authorization": "Bearer tok"}, "env": {"KEY": "v"}},
            ],
        }

    def test_masks_all_secret_fields(self) -> None:
        out = _sanitize_config(self._secretful())
        assert out["llm"]["api_key"] == "***"
        assert out["server_url"]["secret"] == "***"
        assert out["tools"][0]["webhook_secret"] == "***"
        assert out["mcp_servers"][0]["headers"]["Authorization"] == "***"
        assert out["mcp_servers"][0]["env"]["KEY"] == "***"

    def test_preserves_nonsecret_and_shape(self) -> None:
        out = _sanitize_config(self._secretful())
        assert out["llm"]["model"] == "gpt-4o"
        assert out["tools"][1] == {"name": "end_call"}
        assert out["server_url"]["url"] == "https://x"
        # header/env KEYS stay visible; only values masked
        assert "Authorization" in out["mcp_servers"][0]["headers"]

    def test_does_not_mutate_input(self) -> None:
        cfg = self._secretful()
        _sanitize_config(cfg)
        assert cfg["llm"]["api_key"] == "sk-secret"
        assert cfg["tools"][0]["webhook_secret"] == "tool-secret"

    def test_no_secrets_is_noop(self) -> None:
        cfg = {"system_prompt": "hi", "llm": {"provider": "openai", "model": "x"}}
        assert _sanitize_config(cfg)["llm"] == {"provider": "openai", "model": "x"}


@pytest.mark.unit
@pytest.mark.asyncio
class TestRevokeProjectScoping:
    async def _captured_where(self, project_id) -> str:
        from turncall.storage.repositories import api_key_repo

        captured = {}

        class _Result:
            rowcount = 1

        async def fake_execute(query):
            captured["sql"] = str(
                query.compile(compile_kwargs={"literal_binds": False})
            )
            return _Result()

        session = AsyncMock()
        session.execute = fake_execute
        await api_key_repo.revoke_api_key(
            session, uuid.uuid4(), project_id=project_id
        )
        return captured["sql"]

    async def test_scoped_query_filters_project(self) -> None:
        sql = await self._captured_where(uuid.uuid4())
        assert "project_id" in sql

    async def test_unscoped_query_has_no_project_filter(self) -> None:
        sql = await self._captured_where(None)
        assert "project_id" not in sql


@pytest.mark.unit
class TestKeyCreationEndpoint:
    """The escalation vulnerability: a lower-privilege caller minting a higher
    key. Uses dependency overrides so no DB is needed."""

    def _client_as(self, app, role: ProjectRole):
        from unittest.mock import AsyncMock

        from turncall.api.deps import get_session
        from turncall.auth.context import AuthContext
        from turncall.auth.dependencies import resolve_auth_context

        ctx = AuthContext(
            project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=role
        )
        app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
        app.app.dependency_overrides[get_session] = lambda: AsyncMock()
        return ctx

    def test_viewer_cannot_create_keys(self, app) -> None:
        self._client_as(app, ProjectRole.VIEWER)
        r = app.post(
            "/v1/api-keys/authenticated",
            json={"name": "k", "role": "viewer"},
            headers={"Authorization": "Bearer x"},
        )
        assert r.status_code == 403

    def test_developer_cannot_mint_admin(self, app) -> None:
        self._client_as(app, ProjectRole.DEVELOPER)
        r = app.post(
            "/v1/api-keys/authenticated",
            json={"name": "k", "role": "admin"},
            headers={"Authorization": "Bearer x"},
        )
        assert r.status_code == 403
        assert "exceeds your own role" in r.text


@pytest.mark.unit
class TestWriteRbacEnforcement:
    """Mutating routes require a write role; viewers are blocked at the
    dependency before the handler runs (review: RBAC unenforced)."""

    def _as(self, app, role: ProjectRole):
        from unittest.mock import AsyncMock

        from turncall.api.deps import get_session
        from turncall.auth.context import AuthContext
        from turncall.auth.dependencies import resolve_auth_context

        ctx = AuthContext(project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=role)
        app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
        app.app.dependency_overrides[get_session] = lambda: AsyncMock()

    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", "/v1/agents"),
            ("delete", "/v1/agents/" + str(uuid.uuid4())),
            ("post", "/v1/phone-numbers"),
            ("delete", "/v1/takeaways/" + str(uuid.uuid4())),
            ("post", "/v1/webhooks"),
            ("post", "/v1/calls/outbound"),
        ],
    )
    def test_viewer_blocked_on_mutations(self, app, method, path) -> None:
        self._as(app, ProjectRole.VIEWER)
        # WriteAuth runs before body parsing, so no body is needed to prove it.
        r = app.request(method.upper(), path, headers={"Authorization": "Bearer x"})
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"

    def test_developer_passes_write_guard(self, app) -> None:
        # Developer clears WriteAuth; a validation error (422) proves we got
        # past auth into the handler, not a 403.
        self._as(app, ProjectRole.DEVELOPER)
        r = app.post("/v1/agents", json={}, headers={"Authorization": "Bearer x"})
        assert r.status_code != 403
