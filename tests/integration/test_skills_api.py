"""The skill resource and its lifecycle (#423), through the running app.

A skill version is created as a draft, edited while it is one, published
(archiving the name's previous live version) and rolled back (restoring an
archived one and archiving the live one). Lists are newest first in the
paginated envelope. Another project's skill is a 404 on every route. A skill's
tools are the agent tool schema with the agent's rules, a refusal naming the
tool; their webhook secrets are sealed at rest and masked on every read. Every
change is an audit entry with the skill's state. An agent may list only live
skills whose tools take no name already taken, a skill an agent lists is not
deleted, and the agents using a skill are listed with the version each
resolves to (#428). Seam A: the real app over Postgres (`run_app`).
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
                    "delete": (
                        await other.delete(f"/v1/skills/{draft['id']}")
                    ).status_code,
                    "agents": (
                        await other.get("/v1/skills/explain_bill/agents")
                    ).status_code,
                }
                listed = (await other.get("/v1/skills")).json()
                # Its own skill of the same name is its own version 1.
                own = await other.post("/v1/skills", json=_skill())
        finally:
            await _delete_project(other_id)

        assert statuses == {
            "get": 404,
            "put": 404,
            "publish": 404,
            "rollback": 404,
            "delete": 404,
            "agents": 404,
        }
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


async def test_another_projects_knowledge_base_is_refused_at_save(monkeypatch) -> None:
    """#429: a real KB of another project, not just an unknown id."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
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
                theirs = await other.post("/v1/knowledge-bases", json={"name": "x"})
            assert theirs.status_code == 201, theirs.text
            kb_id = theirs.json()["data"]["id"]
            refused = await app.api.post(
                "/v1/skills",
                json=_skill(
                    knowledge_bases=[
                        {
                            "knowledge_base_id": kb_id,
                            "mode": "tool",
                            "tool_description": "Search",
                        }
                    ]
                ),
            )
        finally:
            await _delete_project(other_id)

    assert refused.status_code == 422, refused.text
    assert refused.json()["details"] == {
        "field": "knowledge_bases[0]",
        "knowledge_base_id": kb_id,
    }


async def test_an_auto_mode_knowledge_base_is_a_warning_on_the_skill(
    monkeypatch,
) -> None:
    """#429: accepted, and said on every read — a call does not apply it."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        auto, prompt = [
            (await app.api.post("/v1/knowledge-bases", json={"name": n})).json()[
                "data"
            ]["id"]
            for n in ("auto", "prompt")
        ]
        created = await _create(
            app,
            _skill(
                knowledge_bases=[
                    {"knowledge_base_id": auto},  # the default mode, auto
                    {"knowledge_base_id": prompt, "mode": "prompt"},
                ]
            ),
        )
        read = await _read(app, created["id"])
        listed = (await app.api.get("/v1/skills")).json()["data"]
        plain = await _create(app, _skill("explain_bill"))

    for skill in (created, read, listed[-1]):
        [warning] = skill["warnings"]
        assert warning["code"] == "knowledge_mode_unsupported"
        assert warning["modes"] == ["auto"]
        assert warning["knowledge_base_ids"] == [auto]
    assert plain["warnings"] == []


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


# --- safety at save (#428) ---------------------------------------------------

AGENT = {"system_prompt": "You are Maya."}


async def _publish(app: Harness, body: dict[str, Any]) -> dict[str, Any]:
    created = await _create(app, body)
    return await _post(app, f"/v1/skills/{created['id']}/publish")


async def _agent(
    app: Harness, config: dict[str, Any], name: str = "support"
) -> httpx.Response:
    return await app.api.post("/v1/agents", json={"name": name, "config": config})


def _refusal(response: httpx.Response) -> str:
    assert response.status_code == 422, response.text
    return str(response.json()["error"])


async def test_an_agent_lists_only_live_skills_whose_tools_are_free(
    monkeypatch,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _publish(app, _skill())  # verify_identity: verify_customer
        await _publish(app, _skill("explain_bill", tools=[_tool("get_invoices")]))
        await _publish(app, _skill("payment_promise", tools=[_tool("get_invoices")]))
        await _create(app, _skill("draft_only", tools=[_tool("draft_tool")]))
        own_tool = _tool("get_invoices")

        unknown = await _agent(app, {**AGENT, "skills": [{"skill": "nope"}]})
        draft_only = await _agent(
            app,
            {
                **AGENT,
                "skills": [{"skill": "verify_identity"}, {"skill": "draft_only"}],
            },
        )
        with_own = await _agent(
            app,
            {
                **AGENT,
                "tools": [own_tool],
                "skills": [{"skill": "verify_identity"}, {"skill": "explain_bill"}],
            },
        )
        two_skills = await _agent(
            app,
            {
                **AGENT,
                "skills": [{"skill": "explain_bill"}, {"skill": "payment_promise"}],
            },
        )
        twice = await _agent(
            app,
            {**AGENT, "skills": [{"skill": "explain_bill"}, {"skill": "explain_bill"}]},
        )
        listed_load_skill = await _agent(
            app,
            {
                **AGENT,
                "tools": [{"name": "load_skill", "description": "Load"}],
                "skills": [{"skill": "explain_bill"}],
            },
        )
        fine = await _agent(
            app,
            {
                **AGENT,
                "skills": [{"skill": "verify_identity"}, {"skill": "explain_bill"}],
            },
        )
        assert fine.status_code == 201, fine.text
        agent_id = fine.json()["data"]["id"]
        update = await app.api.put(
            f"/v1/agents/{agent_id}",
            json={
                "config": {
                    **AGENT,
                    "tools": [own_tool],
                    "skills": [{"skill": "explain_bill"}],
                }
            },
        )
        read = (await app.api.get(f"/v1/agents/{agent_id}")).json()["data"]

    assert _refusal(unknown) == "skills[0] 'nope' is not a skill in this project"
    assert unknown.json()["code"] == "invalid_agent_config"
    assert unknown.json()["details"] == {
        "field": "skills[0]",
        "skill": "nope",
        "published": False,
    }
    # A call runs live published versions only, so a draft is no skill yet.
    assert _refusal(draft_only) == (
        "skills[1] 'draft_only' has no published version in this project; "
        "publish one first"
    )
    assert _refusal(with_own) == (
        "skills[1] 'explain_bill' tool 'get_invoices' collides with the "
        "agent's own tool 'get_invoices'"
    )
    assert with_own.json()["details"]["collides_with"] == (
        "the agent's own tool 'get_invoices'"
    )
    assert _refusal(two_skills) == (
        "skills[1] 'payment_promise' tool 'get_invoices' collides with "
        "skills[0] 'explain_bill' tool 'get_invoices'"
    )
    assert _refusal(twice) == (
        "skills[1] 'explain_bill' is listed twice (first as skills[0])"
    )
    assert _refusal(listed_load_skill) == "Validation error"
    assert "'load_skill' is offered automatically" in str(
        listed_load_skill.json()["details"]["errors"]
    )
    # An update is checked the same way, and a refused one changes nothing.
    assert _refusal(update) == (
        "skills[0] 'explain_bill' tool 'get_invoices' collides with the "
        "agent's own tool 'get_invoices'"
    )
    assert read["config"]["skills"] == [
        {"skill": "verify_identity"},
        {"skill": "explain_bill"},
    ]
    assert read["warnings"] == []


async def test_a_skill_in_use_is_not_deleted_and_an_unused_one_is(
    monkeypatch,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        v1 = await _publish(app, _skill())
        v2 = await _create(app, _skill())  # a draft of a skill in use
        unused = await _create(app, _skill("unused"))
        support = await _agent(app, {**AGENT, "skills": [{"skill": "verify_identity"}]})
        assert support.status_code == 201, support.text
        support_id = support.json()["data"]["id"]

        refused = await app.api.delete(f"/v1/skills/{v2['id']}")
        deleted_unused = await app.api.delete(f"/v1/skills/{unused['id']}")
        gone = await app.api.get(f"/v1/skills/{unused['id']}")

        # Deleting the agent archives it: an archived version holds nothing.
        assert (await app.api.delete(f"/v1/agents/{support_id}")).status_code == 200
        deleted_draft = await app.api.delete(f"/v1/skills/{v2['id']}")
        deleted_live = await app.api.delete(f"/v1/skills/{v1['id']}")
        again = await app.api.delete(f"/v1/skills/{v1['id']}")
        audit = await _audit(app, deleted_live)
        left = (await app.api.get("/v1/skills")).json()

    assert refused.status_code == 409, refused.text
    assert "support v1 (draft)" in refused.json()["error"]
    assert refused.json()["details"] == {
        "skill": "verify_identity",
        "agents": [
            {"agent_id": support_id, "name": "support", "version": 1, "state": "draft"}
        ],
    }
    assert deleted_unused.status_code == 200, deleted_unused.text
    assert deleted_unused.json()["data"] == {"deleted": True}
    assert gone.status_code == 404
    assert deleted_draft.status_code == 200, deleted_draft.text
    assert deleted_live.status_code == 200, deleted_live.text
    assert again.status_code == 404
    assert left["total"] == 0

    [entry] = audit
    assert (entry["resource_type"], entry["resource_id"], entry["action"]) == (
        "skill",
        v1["id"],
        "deleted",
    )
    assert entry["changes"]["/name"] == {"before": "verify_identity", "after": None}
    assert entry["changes"]["/state"] == {"before": "published", "after": None}


async def test_the_agents_using_a_skill_and_the_version_each_resolves_to(
    monkeypatch,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _publish(app, _skill())
        await _publish(app, _skill("explain_bill", tools=[_tool("get_invoices")]))
        uses = {"skills": [{"skill": "verify_identity"}]}
        collections = (await _agent(app, {**AGENT, **uses}, "collections")).json()
        await _post(app, f"/v1/agents/{collections['data']['id']}/publish")
        support = (await _agent(app, {**AGENT, **uses}, "support")).json()["data"]
        retired = (await _agent(app, {**AGENT, **uses}, "retired")).json()["data"]
        await app.api.delete(f"/v1/agents/{retired['id']}")
        await _agent(app, {**AGENT, "skills": [{"skill": "explain_bill"}]}, "billing")
        await _agent(app, AGENT, "plain")

        before = await app.api.get("/v1/skills/verify_identity/agents")
        v2 = await _publish(app, _skill())
        after = await app.api.get("/v1/skills/verify_identity/agents")
        draft_name = await _create(app, _skill("draft_only"))
        drafts = await app.api.get("/v1/skills/draft_only/agents")
        missing = await app.api.get("/v1/skills/never_created/agents")
        bad = await app.api.get("/v1/skills/Not-A-Name/agents")

    assert before.status_code == 200, before.text
    rows = before.json()["data"]
    assert [(r["agent_name"], r["agent_version"], r["agent_state"]) for r in rows] == [
        ("collections", 1, "published"),
        ("support", 1, "draft"),
    ]
    assert rows[1] == {
        "agent_id": support["id"],
        "agent_name": "support",
        "agent_version": 1,
        "agent_state": "draft",
        "skill": "verify_identity",
        "resolved_skill_id": rows[1]["resolved_skill_id"],
        "resolved_version": 1,
        "pinned": False,
    }
    # A publish moves every agent following the live version.
    assert [
        (r["resolved_version"], r["resolved_skill_id"]) for r in after.json()["data"]
    ] == [
        (2, v2["id"]),
        (2, v2["id"]),
    ]
    assert drafts.status_code == 200 and drafts.json()["data"] == []
    assert draft_name["state"] == "draft"
    assert missing.status_code == 404
    assert bad.status_code == 422


async def test_an_s2s_agent_with_skills_is_warned_on_the_read(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _publish(app, _skill())
        created = await _agent(
            app,
            {
                **AGENT,
                "pipeline_mode": "s2s",
                "skills": [{"skill": "verify_identity"}],
            },
        )
        assert created.status_code == 201, created.text
        read = (await app.api.get(f"/v1/agents/{created.json()['data']['id']}")).json()

    [warning] = [
        w for w in read["data"]["warnings"] if w["code"] == "skills_ignored_on_s2s"
    ]
    assert warning["skills"] == ["verify_identity"]
    assert "pipeline_mode='cascade'" in warning["message"]


async def _other_projects_skill(app: Harness) -> tuple[str, str]:
    """A published skill version in another project: (project id, version id)."""
    platform = {"X-Platform-Key": PLATFORM_KEY}
    project = await app.api.post(
        "/v1/projects", json={"name": "other"}, headers=platform
    )
    assert project.status_code == 201, project.text
    other_id = project.json()["data"]["id"]
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
        created = await other.post("/v1/skills", json=_skill())
        assert created.status_code == 201, created.text
        skill_id = created.json()["data"]["id"]
        published = await other.post(f"/v1/skills/{skill_id}/publish")
        assert published.status_code == 200, published.text
    return other_id, skill_id


async def test_a_pin_names_a_runnable_version_of_that_skill_in_the_project(
    monkeypatch,
) -> None:
    """#426. A pin runs exactly its version, so at save it must be a published
    or archived version of the named skill in the agent's project, and the
    tools that must be free are that version's, not the live one's."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        v1 = await _publish(app, _skill(tools=[_tool("old_lookup")]))
        v2 = await _publish(app, _skill())  # live; archives v1
        v3 = await _create(app, _skill())  # a draft
        bill = await _publish(app, _skill("explain_bill", tools=[_tool("get_bill")]))
        other_project, foreign = await _other_projects_skill(app)
        try:

            def pinned(version_id: str, **config: Any) -> dict[str, Any]:
                return {
                    **AGENT,
                    **config,
                    "skills": [{"skill": "verify_identity", "version_id": version_id}],
                }

            own = {"tools": [_tool("verify_customer")]}
            following_collides = await _agent(
                app, {**AGENT, **own, "skills": [{"skill": "verify_identity"}]}
            )
            on_v1 = await _agent(app, pinned(v1["id"], **own), "collections")
            v1_collides = await _agent(
                app, pinned(v1["id"], tools=[_tool("old_lookup")])
            )
            draft = await _agent(app, pinned(v3["id"]))
            another_skill = await _agent(app, pinned(bill["id"]))
            nowhere = await _agent(app, pinned(str(uuid.uuid4())))
            another_project = await _agent(app, pinned(foreign))
            follower = await _agent(
                app, {**AGENT, "skills": [{"skill": "verify_identity"}]}, "support"
            )
            assert on_v1.status_code == 201, on_v1.text
            assert follower.status_code == 201, follower.text
            on_v1_id = on_v1.json()["data"]["id"]
            update = await app.api.put(
                f"/v1/agents/{on_v1_id}", json={"config": pinned(bill["id"])}
            )
            read = (await app.api.get(f"/v1/agents/{on_v1_id}")).json()["data"]
            listing = (await app.api.get("/v1/skills/verify_identity/agents")).json()[
                "data"
            ]
            delete_pinned = await app.api.delete(f"/v1/skills/{v1['id']}")
        finally:
            await _delete_project(other_project)

    assert _refusal(following_collides) == (
        "skills[0] 'verify_identity' tool 'verify_customer' collides with the "
        "agent's own tool 'verify_customer'"
    )
    # Pinned to v1, whose tool is not the agent's: the live v2's would be.
    assert _refusal(v1_collides) == (
        "skills[0] 'verify_identity' tool 'old_lookup' collides with the "
        "agent's own tool 'old_lookup'"
    )
    assert _refusal(draft) == (
        f"skills[0] 'verify_identity' version_id '{v3['id']}' is a draft (v3); "
        "pin a published or archived version"
    )
    assert draft.json()["details"] == {
        "field": "skills[0].version_id",
        "skill": "verify_identity",
        "version_id": v3["id"],
    }
    assert _refusal(another_skill) == (
        f"skills[0] 'verify_identity' version_id '{bill['id']}' is a version of "
        "'explain_bill', not of 'verify_identity'"
    )
    not_here = "is not a version of a skill in this project"
    assert _refusal(nowhere).endswith(not_here)
    assert _refusal(another_project) == (
        f"skills[0] 'verify_identity' version_id '{foreign}' {not_here}"
    )
    assert _refusal(update).endswith("not of 'verify_identity'")

    # Stored as sent: a pin keeps its id, an unpinned entry is just the name.
    assert read["config"]["skills"] == [
        {"skill": "verify_identity", "version_id": v1["id"]}
    ]
    assert follower.json()["data"]["config"]["skills"] == [{"skill": "verify_identity"}]
    assert [
        (r["agent_name"], r["resolved_skill_id"], r["resolved_version"], r["pinned"])
        for r in listing
    ] == [
        ("collections", v1["id"], 1, True),
        ("support", v2["id"], 2, False),
    ]
    # A version an agent pins is in use like any other of the name (#428).
    assert delete_pinned.status_code == 409, delete_pinned.text
    assert "collections v1 (draft)" in delete_pinned.json()["error"]
    assert "Remove it from their skills first" in delete_pinned.json()["error"]
