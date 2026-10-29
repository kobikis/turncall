"""The skill resource and its lifecycle (#423), through the running app.

A skill version is created as a draft, edited while it is one, published
(archiving the name's previous live version) and rolled back (restoring an
archived one and archiving the live one). Lists are newest first in the
paginated envelope. Another project's skill is a 404 on every route. A skill's
tools are the agent tool schema with the agent's rules, a refusal naming the
tool; their webhook secrets are sealed at rest and masked on every read. Every
change is an audit entry with the skill's state. Seam A: the real app over
Postgres (`run_app`).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from tests.harness import Harness, Script, run_app
from tests.harness.app import PLATFORM_KEY, _delete_project
from turncall.config.settings import Settings, get_settings
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.sealing import PREFIX

pytestmark = pytest.mark.integration

HEADER = "X-TurnCall-Audit-Id"
SECRET = "skill-webhook-secret-0001"


def _tool(name: str = "verify_customer", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Verify the caller against the account",
        "parameters_schema": {
            "type": "object",
            "properties": {"dob": {"type": "string"}, "last4": {"type": "string"}},
            "required": ["dob", "last4"],
        },
        "webhook_url": "https://crm.example.com/tools/verify",
        **extra,
    }


def _skill(name: str = "verify_identity", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Use before discussing anything account-specific.",
        "instructions": "Ask for the date of birth and the last 4 digits.",
        "tools": [_tool()],
        **extra,
    }


async def _create(app: Harness, body: dict[str, Any]) -> dict[str, Any]:
    response = await app.api.post("/v1/skills", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _post(app: Harness, path: str) -> dict[str, Any]:
    response = await app.api.post(path)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _read(app: Harness, skill_id: str) -> dict[str, Any]:
    response = await app.api.get(f"/v1/skills/{skill_id}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _audit(app: Harness, response: httpx.Response) -> list[dict[str, Any]]:
    """Every entry the request wrote, primary first."""
    request_id = response.headers[HEADER]
    log = await app.api.get("/v1/audit-log", params={"request_id": request_id})
    assert log.status_code == 200, log.text
    return sorted(log.json()["data"], key=lambda e: e["id"] != request_id)


async def test_create_edit_publish_and_roll_back(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        v1 = await _create(app, _skill())
        assert (v1["name"], v1["version"], v1["state"]) == (
            "verify_identity",
            1,
            "draft",
        )
        assert v1["published_at"] is None and v1["knowledge_bases"] == []
        assert v1["tools"][0]["name"] == "verify_customer"

        edited = await app.api.put(
            f"/v1/skills/{v1['id']}", json={"instructions": "Ask for the postcode."}
        )
        assert edited.status_code == 200, edited.text
        edited = edited.json()["data"]
        # Only what was sent changes.
        assert edited["instructions"] == "Ask for the postcode."
        assert edited["description"] == v1["description"]
        assert edited["tools"] == v1["tools"]

        published = await _post(app, f"/v1/skills/{v1['id']}/publish")
        assert published["state"] == "published" and published["published_at"]

        # A published version never changes, and is not published twice.
        refused = await app.api.put(
            f"/v1/skills/{v1['id']}", json={"instructions": "x"}
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["details"] == {"state": "published", "version": 1}
        again = await app.api.post(f"/v1/skills/{v1['id']}/publish")
        assert again.status_code == 409, again.text

        # The same name again is the next version, a draft beside the live one.
        v2 = await _create(app, _skill(instructions="Ask for the DOB only."))
        assert (v2["version"], v2["state"]) == (2, "draft")
        assert (await _read(app, v1["id"]))["state"] == "published"

        await _post(app, f"/v1/skills/{v2['id']}/publish")
        assert (await _read(app, v1["id"]))["state"] == "archived"
        assert (await _read(app, v2["id"]))["state"] == "published"

        # An archived version is not edited either.
        archived_edit = await app.api.put(
            f"/v1/skills/{v1['id']}", json={"instructions": "x"}
        )
        assert archived_edit.status_code == 409, archived_edit.text
        assert archived_edit.json()["details"]["state"] == "archived"

        # Rollback: v1 live again, v2 archived.
        rolled = await _post(app, f"/v1/skills/{v1['id']}/rollback")
        assert rolled["state"] == "published"
        assert rolled["instructions"] == "Ask for the postcode."
        assert (await _read(app, v2["id"]))["state"] == "archived"

        # Only an archived version is rolled back to.
        live = await app.api.post(f"/v1/skills/{v1['id']}/rollback")
        assert live.status_code == 409, live.text
        draft = await _create(app, _skill())
        refused_draft = await app.api.post(f"/v1/skills/{draft['id']}/rollback")
        assert refused_draft.status_code == 409, refused_draft.text

        published_now = await app.api.get(
            "/v1/skills", params={"name": "verify_identity", "state": "published"}
        )
        assert [s["version"] for s in published_now.json()["data"]] == [1]


async def test_lists_are_newest_first_and_paginated(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        a1 = await _create(app, _skill("alpha"))
        b1 = await _create(app, _skill("beta"))
        a2 = await _create(app, _skill("alpha"))

        everything = await app.api.get("/v1/skills")
        body = everything.json()
        assert body["success"] is True
        assert (body["total"], body["page"], body["limit"]) == (3, 1, 50)
        assert [s["id"] for s in body["data"]] == [a2["id"], b1["id"], a1["id"]]

        page_two = await app.api.get("/v1/skills", params={"limit": 2, "page": 2})
        assert [s["id"] for s in page_two.json()["data"]] == [a1["id"]]
        assert page_two.json()["total"] == 3

        alpha = (await app.api.get("/v1/skills", params={"name": "alpha"})).json()
        assert [s["version"] for s in alpha["data"]] == [2, 1]
        assert alpha["total"] == 2

        drafts = await app.api.get("/v1/skills", params={"state": "draft"})
        assert drafts.json()["total"] == 3
        bad = await app.api.get("/v1/skills", params={"state": "live"})
        assert bad.status_code == 422, bad.text


async def test_another_projects_skill_is_not_found(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        draft = await _create(app, _skill())
        published = await _create(app, _skill("explain_bill"))
        await _post(app, f"/v1/skills/{published['id']}/publish")
        archived = await _create(app, _skill("explain_bill"))
        await _post(app, f"/v1/skills/{archived['id']}/publish")  # archives v1

        platform = {"X-Platform-Key": PLATFORM_KEY}
        project = await app.api.post(
            "/v1/projects", json={"name": "other"}, headers=platform
        )
        assert project.status_code == 201, project.text
        other_id = project.json()["data"]["id"]
        try:
            key = await app.api.post(
                "/v1/api-keys",
                params={"project_id": other_id},
                json={"name": "other", "role": "admin"},
                headers=platform,
            )
            assert key.status_code == 201, key.text
            token = key.json()["data"]["raw_key"]
            async with httpx.AsyncClient(
                base_url=app.base_url, headers={"Authorization": f"Bearer {token}"}
            ) as other:
                statuses = {
                    "get": (await other.get(f"/v1/skills/{draft['id']}")).status_code,
                    "put": (
                        await other.put(
                            f"/v1/skills/{draft['id']}", json={"instructions": "x"}
                        )
                    ).status_code,
                    "publish": (
                        await other.post(f"/v1/skills/{draft['id']}/publish")
                    ).status_code,
                    "rollback": (
                        await other.post(f"/v1/skills/{published['id']}/rollback")
                    ).status_code,
                }
                listed = (await other.get("/v1/skills")).json()
                # Its own skill of the same name is its own version 1.
                own = await other.post("/v1/skills", json=_skill())
        finally:
            await _delete_project(other_id)

        assert statuses == {"get": 404, "put": 404, "publish": 404, "rollback": 404}
        assert listed["data"] == [] and listed["total"] == 0
        assert own.status_code == 201 and own.json()["data"]["version"] == 1
        # Nothing of ours moved.
        assert (await _read(app, draft["id"]))["state"] == "draft"
        assert (await _read(app, draft["id"]))["instructions"] == _skill()[
            "instructions"
        ]
        assert (await _read(app, published["id"]))["state"] == "archived"


async def test_tools_follow_the_agent_rules_and_a_refusal_names_the_tool(
    monkeypatch,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        # After the harness subscribed its plain-http webhook sink.
        monkeypatch.setenv("ALLOW_PLAIN_HTTP_URLS", "false")
        get_settings.cache_clear()
        cases = {
            "plain_http": [
                _tool(),
                _tool("get_invoices", webhook_url="http://crm.example.com/x"),
            ],
            "no_webhook": [
                {k: v for k, v in _tool("get_invoices").items() if k != "webhook_url"}
            ],
            "short_secret": [_tool("get_invoices", webhook_secret="short")],
            "bad_name": [_tool("Get-Invoices")],
            "duplicate": [_tool("get_invoices"), _tool("get_invoices")],
            "builtin": [_tool("end_call")],
        }
        refusals = {}
        for case, tools in cases.items():
            response = await app.api.post("/v1/skills", json=_skill(tools=tools))
            assert response.status_code == 422, (case, response.text)
            refusals[case] = str(response.json()["details"])

        draft = await _create(app, _skill())
        edit = await app.api.put(
            f"/v1/skills/{draft['id']}",
            json={"tools": [_tool("get_invoices", webhook_url="http://x.example")]},
        )
        assert edit.status_code == 422, edit.text
        unknown_field = await app.api.post(
            "/v1/skills", json={**_skill(), "mcp_servers": []}
        )
        bad_name = await app.api.post("/v1/skills", json=_skill("Verify Identity"))

    assert "tools[1] 'get_invoices' webhook_url" in refusals["plain_http"]
    assert "https" in refusals["plain_http"]
    assert "'get_invoices'" in refusals["no_webhook"]
    assert "webhook_url" in refusals["no_webhook"]
    assert "tools[0] 'get_invoices' webhook_secret" in refusals["short_secret"]
    assert "tools[0] 'Get-Invoices' name" in refusals["bad_name"]
    assert "unique" in refusals["duplicate"] and "get_invoices" in refusals["duplicate"]
    assert "'end_call' is a built-in" in refusals["builtin"]
    assert "tools[0] 'get_invoices' webhook_url" in str(edit.json()["details"])
    assert unknown_field.status_code == 422, unknown_field.text
    assert bad_name.status_code == 422, bad_name.text


async def test_knowledge_bases_are_the_projects(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        kb = await app.api.post("/v1/knowledge-bases", json={"name": "billing"})
        assert kb.status_code == 201, kb.text
        kb_id = kb.json()["data"]["id"]
        attachment = {
            "knowledge_base_id": kb_id,
            "mode": "tool",
            "top_k": 3,
            "tool_description": "Search the billing FAQ",
        }
        created = await _create(app, _skill(knowledge_bases=[attachment]))
        assert created["knowledge_bases"] == [
            {**attachment, "priority": 0, "similarity_threshold": 0.3}
        ]

        stranger = str(uuid.uuid4())
        missing = await app.api.post(
            "/v1/skills",
            json=_skill(knowledge_bases=[{"knowledge_base_id": stranger}]),
        )
        twice = await app.api.post(
            "/v1/skills", json=_skill(knowledge_bases=[attachment, attachment])
        )
        edit = await app.api.put(
            f"/v1/skills/{created['id']}",
            json={"knowledge_bases": [{"knowledge_base_id": stranger}]},
        )

    assert missing.status_code == 422, missing.text
    assert missing.json()["details"] == {
        "field": "knowledge_bases[0]",
        "knowledge_base_id": stranger,
    }
    assert twice.status_code == 422, twice.text
    assert edit.status_code == 422, edit.text


async def test_a_tool_secret_is_sealed_at_rest_and_masked_on_read(
    monkeypatch,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        created = await app.api.post(
            "/v1/skills", json=_skill(tools=[_tool(webhook_secret=SECRET)])
        )
        assert created.status_code == 201, created.text
        skill_id = created.json()["data"]["id"]
        read = await app.api.get(f"/v1/skills/{skill_id}")
        listed = await app.api.get("/v1/skills")
        log = await app.api.get("/v1/audit-log", params={"resource_type": "skill"})

        engine = create_engine(Settings().database)
        try:
            async with create_session_factory(engine)() as session:
                stored = await session.scalar(
                    sa.text("SELECT tools::text FROM skills WHERE id = :id"),
                    {"id": uuid.UUID(skill_id)},
                )
        finally:
            await engine.dispose()

    assert SECRET not in stored
    assert f'"{PREFIX}' in stored
    for response in (created, read, listed, log):
        assert SECRET not in response.text
    assert read.json()["data"]["tools"][0]["webhook_secret"] == "***"


async def test_every_change_is_an_audit_entry_with_the_skill_state(
    monkeypatch,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        create_1 = await app.api.post(
            "/v1/skills", json=_skill(tools=[_tool(webhook_secret=SECRET)])
        )
        v1 = create_1.json()["data"]["id"]
        edit = await app.api.put(f"/v1/skills/{v1}", json={"description": "New."})
        publish_1 = await app.api.post(f"/v1/skills/{v1}/publish")
        create_2 = await app.api.post("/v1/skills", json=_skill())
        v2 = create_2.json()["data"]["id"]
        publish_2 = await app.api.post(f"/v1/skills/{v2}/publish")
        rollback = await app.api.post(f"/v1/skills/{v1}/rollback")

        entries = {
            name: await _audit(app, response)
            for name, response in {
                "create": create_1,
                "edit": edit,
                "publish_1": publish_1,
                "publish_2": publish_2,
                "rollback": rollback,
            }.items()
        }

    def summary(name: str) -> list[tuple[str, str, str]]:
        return [
            (e["resource_type"], e["resource_id"], e["action"]) for e in entries[name]
        ]

    assert summary("create") == [("skill", v1, "created")]
    created = entries["create"][0]["changes"]
    assert created["/name"] == {"before": None, "after": "verify_identity"}
    assert created["/state"] == {"before": None, "after": "draft"}
    assert created["/tools"]["after"][0]["webhook_secret"] == "***"

    assert summary("edit") == [("skill", v1, "updated")]
    assert entries["edit"][0]["changes"] == {
        "/description": {
            "before": _skill()["description"],
            "after": "New.",
        }
    }

    assert summary("publish_1") == [("skill", v1, "published")]
    assert entries["publish_1"][0]["changes"]["/state"] == {
        "before": "draft",
        "after": "published",
    }
    assert summary("publish_2") == [
        ("skill", v2, "published"),
        ("skill", v1, "archived"),
    ]
    assert summary("rollback") == [
        ("skill", v1, "rolled_back"),
        ("skill", v2, "archived"),
    ]
    assert entries["rollback"][0]["changes"] == {
        "/state": {"before": "archived", "after": "published"}
    }
    assert all(SECRET not in str(e) for es in entries.values() for e in es)
