"""A viewer key changes nothing on an agent or a knowledge base (#335).

What this guards:

- Every mutating route under `/v1/agents` and `/v1/knowledge-bases` refuses
  a viewer key with a 403, before it reads or writes anything. A new route
  that takes `Auth` where it means `WriteAuth` fails here, by name.
- Linking and unlinking an agent's knowledge base, which took `Auth` until
  #335, refuse a viewer key and still work for admin and developer keys.

The end-to-end half is `tests/integration/test_harness_knowledge_link_roles.py`.
"""

import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from turncall.api.audit import AUDIT_HEADER, MUTATING_METHODS, api_routes
from turncall.api.deps import get_session
from turncall.app import create_app
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.enums import ProjectRole

pytestmark = pytest.mark.unit

WRITE_ONLY_PREFIXES = ("/v1/agents", "/v1/knowledge-bases")
PATH_PARAM = re.compile(r"\{[^}]+\}")


def _client(role: ProjectRole, session: Any) -> TestClient:
    app = create_app()
    app.router.lifespan_context = None  # type: ignore[assignment]
    ctx = AuthContext(project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=role)
    app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


def _mutating_routes() -> list[tuple[str, str]]:
    return sorted(
        (method, path)
        for path, route in api_routes(create_app())
        if path.startswith(WRITE_ONLY_PREFIXES)
        for method in route.methods & MUTATING_METHODS
    )


def _touched(session: AsyncMock) -> list[str]:
    """What the request did with the session, but binding an audit trail to
    it (`session.info`), which writes nothing."""
    return [
        str(c)
        for c in session.mock_calls
        if not (c[0] == "info" or c[0].startswith("info."))
    ]


def test_no_mutating_route_on_an_agent_or_knowledge_base_accepts_a_viewer() -> None:
    routes = _mutating_routes()
    # Sanity: the walk found the routes the issue names, and the rest.
    assert {
        ("POST", "/v1/agents"),
        ("PUT", "/v1/agents/{agent_id}"),
        ("POST", "/v1/agents/{agent_id}/knowledge-bases"),
        ("DELETE", "/v1/agents/{agent_id}/knowledge-bases/{kb_id}"),
        ("POST", "/v1/knowledge-bases"),
        ("POST", "/v1/knowledge-bases/{kb_id}/documents"),
    } <= set(routes)
    assert len(routes) >= 13

    accepted = []
    for method, path in routes:
        session = AsyncMock()
        url = PATH_PARAM.sub(lambda _: str(uuid.uuid4()), path)
        # The role check runs before the body is validated: none is needed.
        response = _client(ProjectRole.VIEWER, session).request(method, url)
        body = response.json() if response.content else {}
        if response.status_code != 403 or body.get("code") != "forbidden":
            accepted.append((method, path, response.status_code))
        elif touched := _touched(session):
            accepted.append((method, path, touched))
    assert accepted == [], f"mutating routes a viewer key gets past: {accepted}"


# --- Linking an agent's knowledge base -------------------------------------

AGENT_ID = uuid.uuid4()
KB_ID = uuid.uuid4()
LINK = f"/v1/agents/{AGENT_ID}/knowledge-bases"
UNLINK = f"/v1/agents/{AGENT_ID}/knowledge-bases/{KB_ID}"


def _link_row() -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=AGENT_ID,
        knowledge_base_id=KB_ID,
        mode="prompt",
        priority=0,
        top_k=5,
        similarity_threshold=0.7,
        tool_description=None,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def repos(monkeypatch) -> SimpleNamespace:
    from turncall.storage.repositories import agent_repo, knowledge_repo

    mocks = SimpleNamespace(
        get_agent=AsyncMock(return_value=SimpleNamespace(id=AGENT_ID)),
        get_kb=AsyncMock(return_value=SimpleNamespace(id=KB_ID)),
        link=AsyncMock(return_value=_link_row()),
        links=AsyncMock(return_value=[_link_row()]),
        unlink=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(agent_repo, "get_agent_by_id", mocks.get_agent)
    monkeypatch.setattr(knowledge_repo, "get_knowledge_base_by_id", mocks.get_kb)
    monkeypatch.setattr(knowledge_repo, "link_agent_knowledge_base", mocks.link)
    monkeypatch.setattr(knowledge_repo, "get_agent_knowledge_bases", mocks.links)
    monkeypatch.setattr(knowledge_repo, "unlink_agent_knowledge_base", mocks.unlink)
    return mocks


def _send(client: TestClient, action: str):
    if action == "link":
        return client.post(
            LINK, json={"knowledge_base_id": str(KB_ID), "mode": "prompt"}
        )
    return client.delete(UNLINK)


@pytest.mark.parametrize("action", ["link", "unlink"])
def test_a_viewer_key_cannot_change_an_agents_knowledge_bases(
    repos: SimpleNamespace, action: str
) -> None:
    session = AsyncMock()
    response = _send(_client(ProjectRole.VIEWER, session), action)
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    # Refused before anything is read, let alone written or recorded.
    for repo in vars(repos).values():
        repo.assert_not_awaited()
    assert _touched(session) == []
    assert AUDIT_HEADER not in response.headers


@pytest.mark.parametrize("role", [ProjectRole.ADMIN, ProjectRole.DEVELOPER])
def test_a_writing_key_links_an_agents_knowledge_base(
    repos: SimpleNamespace, role: ProjectRole
) -> None:
    response = _send(_client(role, AsyncMock()), "link")
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["agent_id"] == str(AGENT_ID)
    assert data["knowledge_base_id"] == str(KB_ID)
    repos.link.assert_awaited_once()
    assert AUDIT_HEADER in response.headers


@pytest.mark.parametrize("role", [ProjectRole.ADMIN, ProjectRole.DEVELOPER])
def test_a_writing_key_unlinks_an_agents_knowledge_base(
    repos: SimpleNamespace, role: ProjectRole
) -> None:
    response = _send(_client(role, AsyncMock()), "unlink")
    assert response.status_code == 204, response.text
    repos.unlink.assert_awaited_once()
    assert AUDIT_HEADER in response.headers
