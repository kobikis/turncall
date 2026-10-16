"""A project cannot unlink another project's agent↔KB association.

Regression for the cross-project BOLA a Strix pentest confirmed (2026-10-16):
`DELETE /v1/agents/{agent_id}/knowledge-bases/{kb_id}` scoped only on the two
ids, so any project's key could unlink any other project's link. The sibling
link/list routes already scope by project; unlink now does too.
"""

import httpx
import pytest

from tests.harness import HARNESS_AGENT, run_app
from tests.harness.app import PLATFORM_KEY
from tests.integration.limits_support import SCRIPT

pytestmark = pytest.mark.integration


async def test_other_project_cannot_unlink_an_agents_knowledge_base(monkeypatch):
    async with run_app(
        SCRIPT, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        # Project A (app.api): an agent with a knowledge base linked to it.
        agent_id = await app.create_agent(HARNESS_AGENT)
        kb = await app.api.post("/v1/knowledge-bases", json={"name": "A-secret-kb"})
        assert kb.status_code == 201, kb.text
        kb_id = kb.json()["data"]["id"]
        linked = await app.api.post(
            f"/v1/agents/{agent_id}/knowledge-bases",
            json={"knowledge_base_id": kb_id},
        )
        assert linked.status_code == 201, linked.text

        # Project B: a separate tenant, bootstrapped the way the builder does.
        platform = {"X-Platform-Key": PLATFORM_KEY}
        proj_b = await app.api.post(
            "/v1/projects", json={"name": "B"}, headers=platform
        )
        assert proj_b.status_code == 201, proj_b.text
        key_b = await app.api.post(
            "/v1/api-keys",
            params={"project_id": proj_b.json()["data"]["id"]},
            json={"name": "b", "role": "admin"},
            headers=platform,
        )
        assert key_b.status_code == 201, key_b.text
        token_b = key_b.json()["data"]["raw_key"]

        async with httpx.AsyncClient(
            base_url=app.base_url, headers={"Authorization": f"Bearer {token_b}"}
        ) as client_b:
            # The attack: B unlinks A's agent↔KB by id.
            attack = await client_b.delete(
                f"/v1/agents/{agent_id}/knowledge-bases/{kb_id}"
            )

        # Refused — B does not own the agent, so it reads as not found.
        assert attack.status_code == 404, attack.text

        # And A's link is untouched.
        after = await app.api.get(f"/v1/agents/{agent_id}/knowledge-bases")
        assert after.status_code == 200, after.text
        assert len(after.json()["data"]) == 1
