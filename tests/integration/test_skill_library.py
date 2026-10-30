"""A project's skill library (#432, ADR-0031), through the running app.

Only the platform credential links a project to a library or unlinks it; an
API key, admin included, is refused. The link is refused for the project
itself, a project that is missing or deleted, a library that is linked itself,
and a project others read from — no chains. It is an audit entry by the
platform and shown on the project read, and a purged library leaves its
projects unlinked. A linked project's agents are checked at save against the
library alone: its skills, its drafts, its versions for a pin, its tools for a
collision. Seam A: the real app over Postgres (`run_app`).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from tests.harness import Harness, Script, run_app
from tests.harness.app import PLATFORM_KEY

pytestmark = pytest.mark.integration

AGENT = {"system_prompt": "You are Maya."}
AUDIT = "X-TurnCall-Audit-Id"


def _tool(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": "A tool",
        "parameters_schema": {"type": "object", "properties": {}},
        "webhook_url": "https://crm.example.com/tools/" + name,
    }


def _skill(name: str, *tools: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"Use for {name}.",
        "instructions": f"Do {name}.",
        "tools": [_tool(t) for t in tools],
    }


async def _publish(api: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    created = await api.post("/v1/skills", json=body)
    assert created.status_code == 201, created.text
    published = await api.post(f"/v1/skills/{created.json()['data']['id']}/publish")
    assert published.status_code == 200, published.text
    return dict(published.json()["data"])


async def _project(app: Harness) -> dict[str, Any]:
    read = await app.api.get(f"/v1/projects/{app.project_id}")
    assert read.status_code == 200, read.text
    return dict(read.json()["data"])


def _refused(response: httpx.Response) -> str:
    assert response.status_code == 422, response.text
    return str(response.json()["error"])


async def test_only_the_platform_links_a_project_and_it_is_audited(
    monkeypatch,
) -> None:
    async with (
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        app.another_project("library") as (library, _),
    ):
        path = f"/v1/projects/{app.project_id}/skill-library"
        body = {"library_project_id": library}
        admin_key = await app.api.put(path, json=body)
        wrong_key = await app.api.put(
            path, json=body, headers={"X-Platform-Key": "not-the-key"}
        )
        unlinked = await _project(app)

        linked = await app.link_skill_library(library)
        read = await _project(app)
        log = await app.api.get(
            "/v1/audit-log", params={"request_id": linked.headers[AUDIT]}
        )
        cleared = await app.link_skill_library(None)
        no_body = await app.api.put(
            path, json={}, headers={"X-Platform-Key": PLATFORM_KEY}
        )
        after = await _project(app)

    assert admin_key.status_code == 401, admin_key.text
    assert wrong_key.status_code == 401, wrong_key.text
    assert unlinked["skill_library"] == {"library_project_id": None}

    assert linked.status_code == 200, linked.text
    assert linked.json()["data"]["skill_library"] == {"library_project_id": library}
    assert read["skill_library"] == {"library_project_id": library}
    [entry] = log.json()["data"]
    assert (entry["actor"], entry["api_key_id"]) == ("platform", None)
    assert (entry["resource_type"], entry["resource_id"], entry["action"]) == (
        "project",
        app.project_id,
        "skill_library_set",
    )
    assert entry["changes"] == {
        "/skill_library/library_project_id": {"before": None, "after": library}
    }

    assert cleared.status_code == 200, cleared.text
    assert after["skill_library"] == {"library_project_id": None}
    # A PUT naming nothing is a mistake, never an unlink.
    assert no_body.status_code == 422, no_body.text


async def test_a_link_is_one_level_to_a_live_project(monkeypatch) -> None:
    async with (
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        app.another_project("library") as (library, _),
        app.another_project("reader") as (reader, _),
        app.another_project("deleted") as (deleted, deleted_api),
    ):
        itself = await app.link_skill_library(app.project_id)
        nowhere = await app.link_skill_library(str(uuid.uuid4()))
        gone = await deleted_api.delete(f"/v1/projects/{deleted}")
        assert gone.status_code == 200, gone.text
        soft_deleted = await app.link_skill_library(deleted)

        # The harness project reads from `library`...
        assert (await app.link_skill_library(library)).status_code == 200
        # ...so it cannot be a library itself,
        to_a_linked = await app.link_skill_library(app.project_id, reader)
        # nor can `library`, which it reads from, be linked to another.
        library_linked = await app.link_skill_library(reader, library)
        missing_path = await app.link_skill_library(library, str(uuid.uuid4()))

    assert _refused(itself) == "A project cannot be its own skill library"
    assert itself.json()["details"] == {
        "field": "library_project_id",
        "library_project_id": app.project_id,
    }
    assert "does not exist or is deleted" in _refused(nowhere)
    assert "does not exist or is deleted" in _refused(soft_deleted)
    assert "a skill library cannot be linked to another" in _refused(to_a_linked)
    assert _refused(library_linked) == (
        "This project is the skill library of other projects; a skill library "
        "cannot be linked to another"
    )
    assert missing_path.status_code == 404, missing_path.text


async def test_a_purged_library_leaves_its_projects_unlinked(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        async with app.another_project("library") as (library, _):
            assert (await app.link_skill_library(library)).status_code == 200
            assert (await _project(app))["skill_library"]["library_project_id"]
        # Leaving the block purged it (a hard delete, as the purge job does).
        read = await _project(app)

    assert read["skill_library"] == {"library_project_id": None}


async def test_a_linked_projects_agents_are_checked_against_the_library(
    monkeypatch,
) -> None:
    async with (
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        app.another_project("library") as (library, lib),
    ):
        bill = await _publish(lib, _skill("explain_bill", "get_invoices"))
        draft = await lib.post("/v1/skills", json=_skill("draft_only"))
        assert draft.status_code == 201, draft.text
        # The project's own skill: not where its agents look once it is linked.
        own = await _publish(app.api, _skill("local_only"))
        own_verify = await _publish(app.api, _skill("verify"))
        await _publish(lib, _skill("verify"))

        def agent(*skills: dict[str, Any], tools: tuple[str, ...] = ()) -> Any:
            config = {
                **AGENT,
                "skills": list(skills),
                "tools": [_tool(t) for t in tools],
            }
            return app.api.post("/v1/agents", json={"name": "a", "config": config})

        unlinked_local = await agent({"skill": "local_only"})
        assert unlinked_local.status_code == 201, unlinked_local.text
        unlinked_library = await agent({"skill": "explain_bill"})

        assert (await app.link_skill_library(library)).status_code == 200
        follows = await agent({"skill": "explain_bill"})
        pins = await agent({"skill": "explain_bill", "version_id": bill["id"]})
        local = await agent({"skill": "local_only"})
        drafted = await agent({"skill": "draft_only"})
        collides = await agent({"skill": "explain_bill"}, tools=("get_invoices",))
        pinned_outside = await agent(
            {"skill": "verify", "version_id": own_verify["id"]}
        )

        gone = await lib.delete(f"/v1/projects/{library}")
        assert gone.status_code == 200, gone.text
        library_gone = await agent({"skill": "explain_bill"})

    assert _refused(unlinked_library) == (
        "skills[0] 'explain_bill' is not a skill in this project"
    )
    assert follows.status_code == 201, follows.text
    assert pins.status_code == 201, pins.text
    assert _refused(local) == (
        "skills[0] 'local_only' is not a skill in this project's skill library"
    )
    assert _refused(drafted) == (
        "skills[0] 'draft_only' has no published version in this project's "
        "skill library; publish one first"
    )
    assert _refused(collides) == (
        "skills[0] 'explain_bill' tool 'get_invoices' collides with the agent's "
        "own tool 'get_invoices'"
    )
    assert _refused(pinned_outside) == (
        f"skills[0] 'verify' version_id '{own_verify['id']}' is not a version of "
        "a skill in this project's skill library"
    )
    assert pinned_outside.json()["details"]["field"] == "skills[0].version_id"
    assert _refused(library_gone) == (
        "skills[0] 'explain_bill' cannot be resolved: this project's skill "
        "library is gone"
    )
    assert own["name"] == "local_only"


# --- who uses a library's skill (#434) ----------------------------------------


async def test_a_librarys_skill_counts_the_agents_of_its_linked_projects(
    monkeypatch,
) -> None:
    """The library's delete refusal and agents listing span every project
    linked to it, naming each agent's project; archived versions still hold
    nothing back. A project's own skills, once it is linked, are no agent's."""
    async with (
        run_app(
            Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
        ) as app,
        app.another_project("library") as (library, lib),
    ):
        v1 = await _publish(lib, _skill("verify"))
        v2 = await _publish(lib, _skill("verify"))  # live; archives v1
        own = await _publish(app.api, _skill("verify"))
        mine = {**AGENT, "skills": [{"skill": "verify"}]}
        assert (await app.link_skill_library(library)).status_code == 200
        pinned = {**AGENT, "skills": [{"skill": "verify", "version_id": v1["id"]}]}
        support = await app.api.post(
            "/v1/agents", json={"name": "support", "config": pinned}
        )
        assert support.status_code == 201, support.text
        # Published, then replaced by v2: v1 is archived and counts for nothing.
        for _ in range(2):
            made = await app.api.post(
                "/v1/agents", json={"name": "billing", "config": mine}
            )
            assert made.status_code == 201, made.text
            published = await app.api.post(
                f"/v1/agents/{made.json()['data']['id']}/publish"
            )
            assert published.status_code == 200, published.text
        billing = made.json()["data"]
        librarians = await lib.post("/v1/agents", json={"name": "lib", "config": mine})
        assert librarians.status_code == 201, librarians.text

        listing = await lib.get("/v1/skills/verify/agents")
        refused = await lib.delete(f"/v1/skills/{v2['id']}")
        # The linked project's own `verify`: its agents read the library's.
        own_listing = await app.api.get("/v1/skills/verify/agents")
        own_deleted = await app.api.delete(f"/v1/skills/{own['id']}")

    assert listing.status_code == 200, listing.text
    rows = [
        (r["agent_name"], r["agent_version"], r["agent_state"], r["project_id"])
        for r in listing.json()["data"]
    ]
    assert rows == [
        ("billing", 2, "published", app.project_id),
        ("lib", 1, "draft", library),
        ("support", 1, "draft", app.project_id),
    ]
    by_name = {r["agent_name"]: r for r in listing.json()["data"]}
    assert (by_name["billing"]["resolved_skill_id"], by_name["billing"]["pinned"]) == (
        v2["id"],
        False,
    )
    assert (by_name["support"]["resolved_skill_id"], by_name["support"]["pinned"]) == (
        v1["id"],
        True,
    )

    assert refused.status_code == 409, refused.text
    assert f"support v1 (draft, project {app.project_id})" in refused.json()["error"]
    assert "lib v1 (draft)" in refused.json()["error"]
    agents = refused.json()["details"]["agents"]
    assert [(a["name"], a["version"], a["project_id"]) for a in agents] == [
        ("billing", 2, app.project_id),
        ("lib", 1, library),
        ("support", 1, app.project_id),
    ]
    assert agents[0] == {
        "agent_id": billing["id"],
        "project_id": app.project_id,
        "name": "billing",
        "version": 2,
        "state": "published",
    }

    assert own_listing.json()["data"] == []
    assert own_deleted.status_code == 200, own_deleted.text
