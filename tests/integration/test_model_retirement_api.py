"""The agent read says `gpt-4o-mini` retires (#458), derived each read."""

from __future__ import annotations

import pytest

from tests.harness import Script, run_app

pytestmark = pytest.mark.integration


async def test_the_agent_read_warns_of_a_retiring_model(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        created = await app.api.post(
            "/v1/agents",
            json={
                "name": "support",
                "config": {
                    "system_prompt": "You are Maya.",
                    "llm": {"provider": "openai", "model": "gpt-4o-mini"},
                },
            },
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["data"]["id"]
        read = (await app.api.get(f"/v1/agents/{agent_id}")).json()["data"]

        moved = await app.api.put(
            f"/v1/agents/{agent_id}",
            json={"config": {"system_prompt": "You are Maya.", "llm": {"model": ""}}},
        )
        after = (await app.api.get(f"/v1/agents/{agent_id}")).json()["data"]

    [warning] = [w for w in read["warnings"] if w["code"] == "model_retiring"]
    assert warning["model"] == "gpt-4o-mini"
    assert warning["retires_on"] == "2027-01-14"
    assert warning["field"] == "llm.model"
    assert "gpt-5.4-mini" in warning["message"]
    # Never stored: naming another model takes it away.
    assert moved.status_code == 200, moved.text
    assert [w for w in after["warnings"] if w["code"] == "model_retiring"] == []
