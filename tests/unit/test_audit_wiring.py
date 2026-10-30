"""Every change through the API to an audited resource is recorded (#317,
ADR-0024), and nothing but the janitor changes an entry.

What this guards:

- Every mutating route under `AUDITED_PREFIXES` (agents and their versions,
  publish, rollback and knowledge bases; skills and theirs; credentials;
  phone numbers; API keys; projects and their settings) takes an audit trail. A new one that
  does not fails here, by name.
- A request on such a route that would commit without recording is refused,
  and its change rolled back: the commit itself checks (`before_commit`),
  whoever makes it. With a real database, a route that commits itself:
  `test_harness_audit_log.py`.
- The read is admin-only, and the API has no route that writes an entry.
- No module but the audit repository names the entry's row or writes the
  `audit_log` table in raw SQL.

The end-to-end half is `tests/integration/test_harness_audit_log.py`.
"""

import ast
import re
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.api.audit import (
    AUDIT_HEADER,
    AUDITED_PREFIXES,
    MUTATING_METHODS,
    Audit,
    api_routes,
    uses_audit,
)
from turncall.api.deps import DbSession, get_session
from turncall.api.errors import register_error_handlers
from turncall.app import create_app
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.domain.enums import AuditAction, AuditResource, ProjectRole

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src" / "turncall"
AUDIT_REPO = "storage/repositories/audit_repo.py"
NAMES_THE_ROW = {"storage/models.py", AUDIT_REPO}
WRITES_THE_LOG = re.compile(
    r"\b(insert\s+into|update|delete\s+from|truncate)\s+(\w+\.)?audit_log\b", re.I
)


def _routes() -> list[tuple[str, Any]]:
    return list(api_routes(create_app()))


def test_every_mutating_route_on_an_audited_resource_records() -> None:
    mutating = [
        (path, route)
        for path, route in _routes()
        if path.startswith(AUDITED_PREFIXES) and route.methods & MUTATING_METHODS
    ]
    # Sanity: the walk found the routes the issue names.
    assert {
        "/v1/agents/{agent_id}",
        "/v1/agents/{agent_id}/publish",
        "/v1/agents/{agent_id}/rollback",
        "/v1/agents/{agent_id}/knowledge-bases",
        "/v1/credentials/{provider}",
        "/v1/phone-numbers/{phone_number_id}",
        "/v1/api-keys/{api_key_id}",
        "/v1/projects/{project_id}/retention",
        "/v1/skills",
        "/v1/skills/{skill_id}",
        "/v1/skills/{skill_id}/publish",
        "/v1/skills/{skill_id}/rollback",
    } <= {path for path, _ in mutating}
    assert ("/v1/skills/{skill_id}", {"DELETE"}) in [
        (path, route.methods) for path, route in mutating
    ]
    assert len(mutating) >= 29
    unaudited = [
        (sorted(route.methods), path)
        for path, route in mutating
        if not uses_audit(route)
    ]
    assert unaudited == [], f"mutating routes with no audit trail: {unaudited}"


def test_no_route_writes_an_entry() -> None:
    log_routes = [(path, r.methods) for path, r in _routes() if "audit-log" in path]
    assert log_routes == [("/v1/audit-log", {"GET"})]


def test_the_header_is_documented_on_every_audited_route() -> None:
    app = create_app()
    spec = app.openapi()
    for path, route in api_routes(app):
        if not uses_audit(route):
            continue
        for method in route.methods:
            responses = spec["paths"][path][method.lower()]["responses"]
            status = str(route.status_code or 200)
            assert AUDIT_HEADER in responses[status]["headers"], (method, path)


def _client(role: ProjectRole, session: Any) -> TestClient:
    app = create_app()
    app.router.lifespan_context = None  # type: ignore[assignment]
    ctx = AuthContext(project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=role)
    app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("role", [ProjectRole.DEVELOPER, ProjectRole.VIEWER])
def test_only_an_admin_key_reads_the_log(role: ProjectRole) -> None:
    session = AsyncMock()
    response = _client(role, session).get("/v1/audit-log")
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    session.execute.assert_not_awaited()


def test_an_admin_key_reads_it(monkeypatch) -> None:
    from turncall.storage.repositories import audit_repo

    monkeypatch.setattr(audit_repo, "list_entries", AsyncMock(return_value=[]))
    response = _client(ProjectRole.ADMIN, AsyncMock()).get("/v1/audit-log")
    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": [],
        "next_cursor": None,
        "limit": 50,
    }


class _Outcome:
    def __init__(self) -> None:
        self.state: str | None = None
        self.inserts: list[list[dict]] = []


def _app_with(route_body, monkeypatch) -> tuple[TestClient, _Outcome]:
    """A one-route app on the real trail and a real session (bound to
    nothing), torn down as DbSession is: commit, or roll back on an error."""
    from turncall.services import audit_log

    outcome = _Outcome()
    monkeypatch.setattr(
        audit_log.audit_repo,
        "insert_entries",
        lambda _s, rows: outcome.inserts.append(list(rows)),
    )

    async def get_test_session():
        session = AsyncSession()
        try:
            yield session
            await session.commit()
            outcome.state = "committed"
        except Exception:
            await session.rollback()
            outcome.state = "rolled back"
            raise

    app = FastAPI()
    register_error_handlers(app)

    @app.put("/v1/agents/{agent_id}")
    async def change(agent_id: str, audit: Audit, session: DbSession) -> dict:
        await route_body(audit, session, agent_id)
        return {"success": True}

    ctx = AuthContext(
        project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
    )
    app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.dependency_overrides[get_session] = get_test_session
    return TestClient(app, raise_server_exceptions=False), outcome


def test_a_change_recorded_is_committed_and_named_on_the_response(
    monkeypatch,
) -> None:
    async def records_three(audit, _session, agent_id) -> None:
        for action in (AuditAction.PUBLISHED, AuditAction.ARCHIVED):
            audit.record(AuditResource.AGENT, agent_id, action)
        audit.record(AuditResource.PHONE_NUMBER, "n", AuditAction.UPDATED)

    client, outcome = _app_with(records_three, monkeypatch)
    response = client.put("/v1/agents/a")
    assert response.status_code == 200 and outcome.state == "committed"
    # One id however many entries: the primary one, which names the request.
    primary = uuid.UUID(response.headers[AUDIT_HEADER])
    (rows,) = outcome.inserts  # one INSERT for the request
    assert [r["action"] for r in rows] == ["published", "archived", "updated"]
    assert rows[0]["id"] == primary
    assert {r["request_id"] for r in rows} == {primary}


def test_a_change_not_recorded_is_refused_and_rolled_back(monkeypatch) -> None:
    async def forgets(_audit, _session, _agent_id) -> None:
        return None

    client, outcome = _app_with(forgets, monkeypatch)
    response = client.put("/v1/agents/a")
    assert response.status_code == 500
    assert outcome.state == "rolled back" and outcome.inserts == []
    assert AUDIT_HEADER not in response.headers


def test_a_route_committing_itself_before_recording_is_refused(monkeypatch) -> None:
    """The check is the commit's, not the end of the request's: a route that
    commits its change and records after (or never) commits nothing."""
    commits: list[str] = []

    async def commits_then_records(audit, session, agent_id) -> None:
        try:
            await session.commit()
        except Exception as exc:
            commits.append(type(exc).__name__)
            raise
        audit.record(AuditResource.AGENT, agent_id, AuditAction.UPDATED)

    client, outcome = _app_with(commits_then_records, monkeypatch)
    response = client.put("/v1/agents/a")
    assert response.status_code == 500
    assert commits == ["AuditEntryMissing"]
    assert outcome.state == "rolled back" and outcome.inserts == []


def test_only_the_audit_repository_writes_the_log() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        source = path.read_text()
        tree = ast.parse(source)
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        if "AuditEntryRow" in names and rel not in NAMES_THE_ROW:
            offenders.append(f"{rel}: names AuditEntryRow")
        if WRITES_THE_LOG.search(source):
            offenders.append(f"{rel}: writes audit_log in SQL")
    assert offenders == [], offenders
    # The trail itself goes through the repository.
    assert "audit_repo.insert_entries" in (SRC / "services/audit_log.py").read_text()
