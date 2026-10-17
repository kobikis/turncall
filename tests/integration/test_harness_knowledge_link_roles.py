"""Who may link and unlink an agent's knowledge bases, end to end (#335).

An admin or developer key links and unlinks; a viewer key gets a 403 on
both, and the agent's links and the audit log are as they were. Driven
through Seam A (the real app, stub providers, a fake carrier). The route
enumeration guard is `tests/unit/test_write_role_wiring.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, Harness, Script, run_app

pytestmark = pytest.mark.integration


async def _headers(app: Harness, role: str) -> dict[str, str]:
    created = await app.api.post(
        "/v1/api-keys/authenticated", json={"name": role, "role": role}
    )
    assert created.status_code == 201, created.text
    return {"Authorization": f"Bearer {created.json()['data']['raw_key']}"}


async def _links(app: Harness, agent_id: str) -> list[str]:
    response = await app.api.get(f"/v1/agents/{agent_id}/knowledge-bases")
    assert response.status_code == 200, response.text
    return [link["knowledge_base_id"] for link in response.json()["data"]]


async def _log(app: Harness) -> list[dict[str, Any]]:
    response = await app.api.get("/v1/audit-log")
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_a_viewer_key_cannot_link_or_unlink(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        viewer = await _headers(app, "viewer")
        agent = await app.api.post(
            "/v1/agents", json={"name": "support", "config": HARNESS_AGENT}
        )
        agent_id = agent.json()["data"]["id"]
        linked_kb, other_kb = [
            (await app.api.post("/v1/knowledge-bases", json={"name": n})).json()[
                "data"
            ]["id"]
            for n in ("faq", "pricing")
        ]
        linked = await app.api.post(
            f"/v1/agents/{agent_id}/knowledge-bases",
            json={"knowledge_base_id": linked_kb, "mode": "prompt"},
        )
        assert linked.status_code == 201, linked.text
        log_before = await _log(app)

        link = await app.api.post(
            f"/v1/agents/{agent_id}/knowledge-bases",
            json={"knowledge_base_id": other_kb, "mode": "prompt"},
            headers=viewer,
        )
        unlink = await app.api.delete(
            f"/v1/agents/{agent_id}/knowledge-bases/{linked_kb}", headers=viewer
        )
        links_after = await _links(app, agent_id)
        log_after = await _log(app)

    for refused in (link, unlink):
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "forbidden"
    # Nothing changed: the link the viewer tried to remove is there, the one
    # it tried to add is not, and neither attempt is an entry.
    assert links_after == [linked_kb]
    assert log_after == log_before


@pytest.mark.parametrize("role", ["admin", "developer"])
async def test_a_writing_key_links_and_unlinks(monkeypatch, role: str) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        headers = await _headers(app, role)
        agent = await app.api.post(
            "/v1/agents", json={"name": "support", "config": HARNESS_AGENT}
        )
        agent_id = agent.json()["data"]["id"]
        kb = await app.api.post("/v1/knowledge-bases", json={"name": "faq"})
        kb_id = kb.json()["data"]["id"]

        link = await app.api.post(
            f"/v1/agents/{agent_id}/knowledge-bases",
            json={"knowledge_base_id": kb_id, "mode": "prompt"},
            headers=headers,
        )
        links_linked = await _links(app, agent_id)
        unlink = await app.api.delete(
            f"/v1/agents/{agent_id}/knowledge-bases/{kb_id}", headers=headers
        )
        links_unlinked = await _links(app, agent_id)
        actions = [e["action"] for e in await _log(app)][:2]

    assert link.status_code == 201, link.text
    assert links_linked == [kb_id]
    assert unlink.status_code == 204, unlink.text
    assert links_unlinked == []
    assert actions == ["knowledge_base_unlinked", "knowledge_base_linked"]
