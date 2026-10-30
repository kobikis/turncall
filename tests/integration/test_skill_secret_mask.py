"""A skill tool's masked secret sent back means "unchanged" (builder-web#125).

Every read masks a tool's `webhook_secret` as `***`, so a client that makes a
new version from a read one, or saves a draft's tools as it read them, can
only send the mask back. That keeps the secret the tool's name already holds
in the skill's versions. A mask with no secret behind it is a 422 naming the
tool, never a stored `***`. Seam A: the real app over Postgres (`run_app`).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.harness import Harness, Script, run_app
from turncall.config.settings import Settings
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import SkillRow

pytestmark = pytest.mark.integration

SECRET = "skill-webhook-secret-0001"


def _tool(name: str = "verify_customer", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Verify the caller against the account",
        "parameters_schema": {"type": "object", "properties": {}},
        "webhook_url": "https://crm.example.com/tools/verify",
        **extra,
    }


def _skill(tools: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": "verify_identity",
        "description": "Use before discussing anything account-specific.",
        "instructions": "Ask for the date of birth and the last 4 digits.",
        "tools": tools,
    }


async def _create(app: Harness, body: dict[str, Any]) -> dict[str, Any]:
    response = await app.api.post("/v1/skills", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _stored_secrets(skill_id: str) -> list[str | None]:
    engine = create_engine(Settings().database)
    try:
        async with create_session_factory(engine)() as session:
            row = await session.get(SkillRow, uuid.UUID(skill_id))
            assert row is not None
            return [t.get("webhook_secret") for t in row.tools]
    finally:
        await engine.dispose()


async def test_a_new_version_from_a_read_one_keeps_the_secret(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        live = await _create(app, _skill([_tool(webhook_secret=SECRET)]))
        assert live["tools"][0]["webhook_secret"] == "***"

        again = await _create(app, _skill(live["tools"]))

        assert await _stored_secrets(again["id"]) == [SECRET]
        assert again["tools"][0]["webhook_secret"] == "***"


async def test_a_draft_saved_with_its_read_tools_keeps_the_secret(
    monkeypatch,
) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        draft = await _create(app, _skill([_tool(webhook_secret=SECRET)]))

        saved = await app.api.put(
            f"/v1/skills/{draft['id']}",
            json={"description": "Changed.", "tools": draft["tools"]},
        )

        assert saved.status_code == 200, saved.text
        assert await _stored_secrets(draft["id"]) == [SECRET]


async def test_a_mask_with_no_secret_behind_it_is_refused(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        await _create(app, _skill([_tool(webhook_secret=SECRET)]))

        refused = await app.api.post(
            "/v1/skills",
            json=_skill([_tool(), _tool("get_invoices", webhook_secret="***")]),
        )

    assert refused.status_code == 422, refused.text
    body = refused.json()
    assert "tools[1] 'get_invoices' webhook_secret" in body["error"]
    assert body["details"]["field"] == "tools[1].webhook_secret"
