"""The audit log, end to end (#317, ADR-0024).

Every change made through the API to an agent, a project credential, a phone
number, an API key or a project setting is an entry: which key made it, what
it did to which resource, and what changed, with no secret in it. Admin keys
read it; a developer or viewer key cannot. Each change's response names its
primary entry in `X-TurnCall-Audit-Id`, and `?request_id=` with that id reads
back everything the request wrote. A request that would commit unrecorded,
whoever commits it, commits nothing. Driven through Seam A (the real app, stub
providers, a fake carrier). The store itself (append-only, retention) is
`test_audit_log_store.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from tests.harness import HARNESS_AGENT, Harness, Script, run_app
from turncall.config.settings import Settings
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import AuditEntryRow

pytestmark = pytest.mark.integration

HEADER = "X-TurnCall-Audit-Id"
CREDENTIAL = "sk-project-credential-0001"
CREDENTIAL_2 = "sk-project-credential-0002"
AGENT_KEY = "sk-agent-inline-key-0001"
AGENT_KEY_2 = "sk-agent-inline-key-0002"
SERVER_SECRET = "server-events-secret-0001"
TOOL_SECRET = "tool-webhook-secret-0001"
MCP_TOKEN = "Bearer mcp-token-0001"


def _id(response: httpx.Response) -> str:
    """The entry the response names: one, however many the request wrote."""
    assert response.status_code < 300, response.text
    return str(uuid.UUID(response.headers[HEADER]))


async def _key(app: Harness, role: str) -> tuple[dict[str, str], dict[str, Any]]:
    """A new key of `role`: its headers, and the create response's data."""
    created = await app.api.post(
        "/v1/api-keys/authenticated", json={"name": role, "role": role}
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    return {"Authorization": f"Bearer {data['raw_key']}"}, data


async def _log(app: Harness, **params: Any) -> list[dict[str, Any]]:
    response = await app.api.get("/v1/audit-log", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _agent(app: Harness, name: str, config: dict[str, Any]) -> str:
    created = await app.api.post("/v1/agents", json={"name": name, "config": config})
    assert created.status_code == 201, created.text
    return created.json()["data"]["id"]


async def test_an_agent_update_and_a_credential_change_are_recorded(
    monkeypatch,
) -> None:
    """The issue's harness: both changes are entries keyed by the admin key,
    and neither a developer nor a viewer key can read them."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        (admin,) = (await app.api.get("/v1/api-keys")).json()["data"]
        agent_id = await _agent(app, "support", HARNESS_AGENT)
        updated = await app.api.put(
            f"/v1/agents/{agent_id}",
            json={"config": {**HARNESS_AGENT, "system_prompt": "Be brief."}},
        )
        stored = await app.api.put(
            "/v1/credentials/openai", json={"api_key": CREDENTIAL}
        )
        replaced = await app.api.put(
            "/v1/credentials/openai", json={"api_key": CREDENTIAL_2}
        )
        log = await app.api.get("/v1/audit-log")
        refused = {}
        for role in ("developer", "viewer"):
            headers, _ = await _key(app, role)
            response = await app.api.get("/v1/audit-log", headers=headers)
            refused[role] = (response.status_code, response.json()["code"])

    entries = log.json()["data"]
    by_id = {e["id"]: e for e in entries}
    update = by_id[_id(updated)]
    assert update["api_key_id"] == admin["id"] and update["actor"] == "api_key"
    assert update["project_id"] == app.project_id
    assert (update["resource_type"], update["resource_id"], update["action"]) == (
        "agent",
        agent_id,
        "updated",
    )
    assert update["changes"] == {
        "/config/system_prompt": {
            "before": HARNESS_AGENT["system_prompt"],
            "after": "Be brief.",
        }
    }
    assert datetime.fromisoformat(update["created_at"]) > datetime.now(UTC) - timedelta(
        minutes=5
    )

    first, second = by_id[_id(stored)], by_id[_id(replaced)]
    assert (first["resource_type"], first["resource_id"]) == ("credential", "openai")
    assert first["action"] == "created" and second["action"] == "updated"
    assert first["changes"] == {"/value": {"before": None, "after": "***"}}
    assert second["changes"] == {"/value": {"before": "***", "after": "***"}}
    assert first["api_key_id"] == second["api_key_id"] == admin["id"]
    assert CREDENTIAL not in log.text and CREDENTIAL_2 not in log.text

    # Newest first: the second credential write heads the log.
    assert [e["id"] for e in entries[:2]] == [second["id"], first["id"]]
    assert refused == {"developer": (403, "forbidden"), "viewer": (403, "forbidden")}


async def test_every_audited_resource_is_recorded(monkeypatch) -> None:
    """Agents (versions, publish, rollback, knowledge bases, delete), API keys,
    phone numbers, credentials and project settings, and the platform
    credential's bootstrap of the project and its first key."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        pid = app.project_id
        bootstrap = await _log(app)
        _, dev = await _key(app, "developer")
        revoked = await app.api.delete(f"/v1/api-keys/{dev['id']}")

        limits = await app.api.put(f"/v1/projects/{pid}/limits", json={"live_calls": 3})
        redaction = await app.api.put(
            f"/v1/projects/{pid}/redaction", json={"enabled": False}
        )
        retention = await app.api.put(
            f"/v1/projects/{pid}/retention", json={"transcripts_days": 30}
        )

        v1 = await _agent(app, "support", HARNESS_AGENT)
        published_v1 = await app.api.post(f"/v1/agents/{v1}/publish")
        number = await app.bind_number(v1)
        numbers = (await app.api.get("/v1/phone-numbers")).json()["data"]
        number_id = next(n["id"] for n in numbers if n["e164_number"] == number)
        v2 = await _agent(app, "support", {**HARNESS_AGENT, "first_message": "Hi."})
        published_v2 = await app.api.post(f"/v1/agents/{v2}/publish")
        published_v2_all = await _log(app, request_id=_id(published_v2))
        rolled_back = await app.api.post(f"/v1/agents/{v1}/rollback")
        rolled_back_all = await _log(app, request_id=_id(rolled_back))

        kb = await app.api.post("/v1/knowledge-bases", json={"name": "faq"})
        kb_id = kb.json()["data"]["id"]
        linked = await app.api.post(
            f"/v1/agents/{v1}/knowledge-bases",
            json={"knowledge_base_id": kb_id, "mode": "prompt"},
        )
        unlinked = await app.api.delete(f"/v1/agents/{v1}/knowledge-bases/{kb_id}")

        number_update = await app.api.put(
            f"/v1/phone-numbers/{number_id}",
            json={
                "routing_target_type": "agent",
                "routing_target_id": v1,
                "sms_enabled": True,
            },
        )
        routing = await app.api.put(
            f"/v1/phone-numbers/{number_id}/routing",
            json={
                "weights": [
                    {"agent_id": v1, "weight": 50},
                    {"agent_id": v2, "weight": 50},
                ]
            },
        )
        routing_cleared = await app.api.delete(f"/v1/phone-numbers/{number_id}/routing")
        unbound = await app.api.delete(f"/v1/phone-numbers/{number_id}")
        await app.api.put("/v1/credentials/deepgram", json={"api_key": CREDENTIAL})
        credential_deleted = await app.api.delete("/v1/credentials/deepgram")
        nothing_deleted = await app.api.delete("/v1/credentials/deepgram")
        draft = await _agent(app, "other", HARNESS_AGENT)
        agent_deleted = await app.api.delete(f"/v1/agents/{draft}")
        log = {e["id"]: e for e in await _log(app, limit=200)}

    def entry(response: httpx.Response) -> tuple[str, str, str]:
        e = log[_id(response)]
        return e["resource_type"], e["resource_id"], e["action"]

    def changes(response: httpx.Response) -> dict[str, Any]:
        return log[_id(response)]["changes"]

    def request(entries: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
        return {(e["resource_type"], e["resource_id"], e["action"]) for e in entries}

    # The bootstrap: no API key made these, the platform credential did.
    project, first_key = sorted(
        bootstrap, key=lambda e: e["resource_type"] != "project"
    )
    assert (project["resource_type"], project["action"]) == ("project", "created")
    assert project["resource_id"] == pid and project["actor"] == "platform"
    assert project["api_key_id"] is None
    assert project["changes"]["/name"] == {"before": None, "after": "harness"}
    assert (first_key["resource_type"], first_key["action"]) == ("api_key", "created")
    assert first_key["actor"] == "platform" and first_key["api_key_id"] is None
    assert first_key["changes"]["/role"] == {"before": None, "after": "admin"}

    assert entry(revoked) == ("api_key", dev["id"], "revoked")
    assert changes(revoked)["/revoked_at"]["before"] is None
    assert changes(revoked)["/revoked_at"]["after"]

    assert entry(limits) == ("project", pid, "limits_set")
    assert changes(limits) == {"/limits/live_calls": {"before": None, "after": 3}}
    assert entry(redaction) == ("project", pid, "redaction_set")
    assert entry(retention) == ("project", pid, "retention_set")
    assert changes(retention) == {
        "/retention/transcripts_days": {"before": None, "after": 30}
    }

    assert entry(published_v1) == ("agent", v1, "published")
    assert changes(published_v1)["/state"] == {
        "before": "draft",
        "after": "published",
    }
    # A publish writes an entry for each thing it changed: the version
    # published (the one the response names), the one it archived, and the
    # number it re-pointed. Each carries the request's id: the named one's.
    assert entry(published_v2) == ("agent", v2, "published")
    assert request(published_v2_all) == {
        ("agent", v2, "published"),
        ("agent", v1, "archived"),
        ("phone_number", number_id, "updated"),
    }
    assert {e["request_id"] for e in published_v2_all} == {_id(published_v2)}
    (repointed,) = [e for e in published_v2_all if e["resource_id"] == number_id]
    assert repointed["changes"] == {"/routing_target_id": {"before": v1, "after": v2}}
    assert entry(rolled_back) == ("agent", v1, "rolled_back")
    assert request(rolled_back_all) == {
        ("agent", v1, "rolled_back"),
        ("agent", v2, "archived"),
        ("phone_number", number_id, "updated"),
    }

    assert entry(linked) == ("agent", v1, "knowledge_base_linked")
    link = changes(linked)["/knowledge_base"]["after"]
    assert link["knowledge_base_id"] == kb_id and link["mode"] == "prompt"
    assert entry(unlinked) == ("agent", v1, "knowledge_base_unlinked")
    assert changes(unlinked)["/knowledge_base"]["before"] == link

    assert entry(number_update) == ("phone_number", number_id, "updated")
    assert changes(number_update)["/sms_enabled"] == {
        "before": False,
        "after": True,
    }
    assert entry(routing) == ("phone_number", number_id, "routing_set")
    assert entry(routing_cleared) == ("phone_number", number_id, "routing_cleared")
    assert entry(unbound) == ("phone_number", number_id, "deleted")
    assert changes(unbound)["/e164_number"] == {
        "before": number,
        "after": None,
    }
    assert entry(credential_deleted) == ("credential", "deepgram", "deleted")
    assert changes(credential_deleted) == {"/value": {"before": "***", "after": None}}
    # Idempotent and audited: nothing there to delete, nothing changed.
    assert entry(nothing_deleted) == ("credential", "deepgram", "deleted")
    assert changes(nothing_deleted) == {}
    assert entry(agent_deleted) == ("agent", draft, "deleted")
    assert changes(agent_deleted)["/state"] == {
        "before": "draft",
        "after": "archived",
    }


async def test_no_secret_reaches_the_log(monkeypatch) -> None:
    """Credential values, an agent's inline keys and signing secrets, a
    number's call-init secret and an API key's material: none is recorded,
    and a changed secret still reads as changed."""
    config = {
        **HARNESS_AGENT,
        "llm": {"provider": "openai", "api_key": AGENT_KEY},
        "server_url": {"url": "https://example.com/events", "secret": SERVER_SECRET},
        "mcp_servers": [
            {
                "name": "crm",
                "transport": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": MCP_TOKEN},
            }
        ],
    }
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await _agent(app, "secretive", config)
        rotated = await app.api.put(
            f"/v1/agents/{agent_id}",
            json={
                "config": {**config, "llm": {**config["llm"], "api_key": AGENT_KEY_2}}
            },
        )
        await app.api.put("/v1/credentials/openai", json={"api_key": CREDENTIAL})
        await app.api.delete("/v1/credentials/openai")
        _, key = await _key(app, "developer")
        number = await app.api.post(
            "/v1/phone-numbers",
            json={
                "carrier": "asterisk",
                "e164_number": "+1555" + str(uuid.uuid4().int)[:7],
                "routing_target_type": "webhook",
                "server_url": "https://example.com/call-init",
            },
        )
        assert number.status_code == 201, number.text
        number_secret = number.json()["data"]["server_url_secret"]
        log = await app.api.get("/v1/audit-log", params={"limit": 200})

    assert log.status_code == 200
    for secret in (
        CREDENTIAL,
        AGENT_KEY,
        AGENT_KEY_2,
        SERVER_SECRET,
        MCP_TOKEN,
        number_secret,
        key["raw_key"],
        key["key_prefix"],
    ):
        assert secret not in log.text, secret
    entries = {e["id"]: e for e in log.json()["data"]}
    assert entries[_id(rotated)]["changes"] == {
        "/config/llm/api_key": {"before": "***", "after": "***"}
    }
    created_number = entries[_id(number)]["changes"]
    assert created_number["/server_url_secret"] == {"before": None, "after": "***"}


async def test_a_change_rolled_back_leaves_no_entry(monkeypatch) -> None:
    """The entry is written in the change's transaction: a developer key's
    agent with its own retention is refused after the write (#316), and
    neither the agent nor its entry is kept."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        headers, _ = await _key(app, "developer")
        before = await _log(app, resource_type="agent")
        refused = await app.api.post(
            "/v1/agents",
            json={
                "name": "keeps",
                "config": {**HARNESS_AGENT, "retention": {"transcripts_days": 1}},
            },
            headers=headers,
        )
        after = await _log(app, resource_type="agent")
        agents = (await app.api.get("/v1/agents")).json()["data"]

    assert refused.status_code == 403
    assert HEADER not in refused.headers
    assert before == after == [] and agents == []


async def test_a_change_not_recorded_is_not_saved(monkeypatch) -> None:
    """A route that forgets its entry commits nothing, whoever commits: an
    agent update (DbSession commits at the end), and a credential and a
    redaction setting (the route commits itself, before its side effect)."""
    from turncall.services.audit_log import AuditTrail

    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        pid = app.project_id
        agent_id = await _agent(app, "support", HARNESS_AGENT)
        before = await _log(app, limit=200)
        with pytest.MonkeyPatch.context() as forgetful:
            forgetful.setattr(AuditTrail, "record", lambda *_a, **_k: uuid.uuid4())
            responses = [
                await app.api.put(
                    f"/v1/agents/{agent_id}",
                    json={"config": {**HARNESS_AGENT, "system_prompt": "Unsaved."}},
                ),
                await app.api.put(
                    "/v1/credentials/openai", json={"api_key": CREDENTIAL}
                ),
                await app.api.put(
                    f"/v1/projects/{pid}/redaction", json={"enabled": True}
                ),
            ]
        agent = (await app.api.get(f"/v1/agents/{agent_id}")).json()["data"]
        credentials = (await app.api.get("/v1/credentials")).json()["data"]
        project = (await app.api.get(f"/v1/projects/{pid}")).json()["data"]
        after = await _log(app, limit=200)

    assert [r.status_code for r in responses] == [500, 500, 500]
    assert all(HEADER not in r.headers for r in responses)
    assert agent["config"]["system_prompt"] == HARNESS_AGENT["system_prompt"]
    assert credentials == []
    assert project["redaction"]["enabled"] is False
    assert after == before


async def test_filters_and_pages(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        a = await _agent(app, "a", HARNESS_AGENT)
        b = await _agent(app, "b", HARNESS_AGENT)
        for prompt in ("one", "two", "three"):
            await app.api.put(
                f"/v1/agents/{a}",
                json={"config": {**HARNESS_AGENT, "system_prompt": prompt}},
            )
        everything = await _log(app, limit=200)
        of_a = await _log(app, resource_type="agent", resource_id=a)
        updates = await _log(app, action="updated")
        credentials = await _log(app, resource_type="credential")
        middle = everything[3]["created_at"]
        since = await _log(app, since=middle, limit=200)
        until = await _log(app, until=middle, limit=200)
        pages: list[list[str]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = (await app.api.get("/v1/audit-log", params=params)).json()
            pages.append([e["id"] for e in page["data"]])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        bad_cursor = await app.api.get("/v1/audit-log", params={"cursor": "nope"})
        bad_type = await app.api.get("/v1/audit-log", params={"resource_type": "call"})

    assert [e["action"] for e in of_a] == ["updated"] * 3 + ["created"]
    assert {e["resource_id"] for e in of_a} == {a}
    assert [e["changes"]["/config/system_prompt"]["after"] for e in updates] == [
        "three",
        "two",
        "one",
    ]
    assert credentials == []
    assert b in {e["resource_id"] for e in everything}
    stamps = [e["created_at"] for e in everything]
    assert stamps == sorted(stamps, reverse=True)
    assert all(e["created_at"] >= middle for e in since)
    assert all(e["created_at"] < middle for e in until)
    assert len(since) + len(until) == len(everything)
    assert [i for page in pages for i in page] == [e["id"] for e in everything]
    assert all(len(page) <= 2 for page in pages)
    assert bad_cursor.status_code == 400
    assert bad_type.status_code == 422


async def test_no_route_changes_an_entry(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        (entry, *_) = await _log(app)
        tried = [
            (await app.api.request(method, f"/v1/audit-log/{entry['id']}")).status_code
            for method in ("PUT", "PATCH", "DELETE")
        ]
        tried += [
            (await app.api.request(method, "/v1/audit-log")).status_code
            for method in ("POST", "PUT", "PATCH", "DELETE")
        ]
        still = await _log(app)

    assert set(tried) <= {404, 405}, tried
    assert entry in still


async def test_a_deleted_project_keeps_its_trail(monkeypatch) -> None:
    """The project's deletion is an entry, and its entries outlive it: the
    soft delete and the purge's hard delete (the harness's teardown) both."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        pid = app.project_id
        await _agent(app, "a", HARNESS_AGENT)
        deleted = await app.api.delete(f"/v1/projects/{pid}")
        read_after = await app.api.get("/v1/audit-log")
    # run_app has hard-deleted the project row by now.

    engine = create_engine(Settings().database)
    try:
        async with create_session_factory(engine)() as session:
            rows = (
                await session.execute(
                    select(AuditEntryRow).where(
                        AuditEntryRow.project_id == uuid.UUID(pid)
                    )
                )
            ).scalars()
            kept = {(r.resource_type, r.action): r for r in rows}
            left = await session.scalar(select(func.count()).select_from(AuditEntryRow))
    finally:
        await engine.dispose()

    assert deleted.status_code == 200
    assert str(kept[("project", "deleted")].id) == _id(deleted)
    assert kept[("project", "deleted")].changes["/deleted_at"]["before"] is None
    assert kept[("project", "deleted")].changes["/deleted_at"]["after"]
    assert ("agent", "created") in kept and ("project", "created") in kept
    assert left
    # Its keys stop working with it, so only the database reads them now.
    assert read_after.status_code == 401
