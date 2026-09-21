"""An agent's secrets must not leave through an eval run (#91).

`_sanitize_config` guards the agent endpoints; the eval feature added two more
paths carrying the same `config_blob` — the run response (gated on `Auth`, so a
*viewer* key reads it) and the `eval.run.*` webhook payloads (a lower trust
boundary still). These tests fail if either path stops going through the masker,
and — because they assert against `sanitize_config`'s own output rather than a
hand-written list — they cover any secret field added to it later.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from turncall.api.v1.schemas.evals import EvalRunResponse
from turncall.domain.config_secrets import sanitize_config

# One sentinel per secret-bearing field `sanitize_config` documents. Searched
# for as a substring of the serialized payload, so a new exit that forgets to
# mask is caught wherever in the shape it appears.
SECRETFUL = {
    "system_prompt": "hi",
    "llm": {"provider": "openai", "model": "gpt-4o", "api_key": "sk-LEAK-llm"},
    "aws": {
        "region": "us-east-1",
        "secret_access_key": "sk-LEAK-aws",
        "session_token": "sk-LEAK-token",
    },
    "server_url": {"url": "https://x", "secret": "sk-LEAK-server"},
    "tools": [{"name": "book", "webhook_secret": "sk-LEAK-tool"}],
    "mcp_servers": [
        {
            "name": "crm",
            "headers": {"Authorization": "sk-LEAK-header"},
            "env": {"KEY": "sk-LEAK-env"},
        }
    ],
}


def _leaks(payload: object) -> list[str]:
    blob = json.dumps(payload, default=str)
    return [v for v in _secret_values(SECRETFUL) if v in blob]


def _secret_values(node: object) -> list[str]:
    if isinstance(node, dict):
        return [v for child in node.values() for v in _secret_values(child)]
    if isinstance(node, list):
        return [v for child in node for v in _secret_values(child)]
    return [node] if isinstance(node, str) and node.startswith("sk-LEAK") else []


def _row(**over):
    base = dict(
        id=uuid4(),
        project_id=uuid4(),
        status="passed",
        modality="text",
        kind="script",
        iterations=1,
        scenario_name="greets",
        scenario_id=uuid4(),
        batch_id=uuid4(),
        target={"type": "inline", "agent": SECRETFUL},
        resolved_scenario={"definition": {"turns": []}},
        passed_count=1,
        failed_count=0,
        error=None,
        results=[],
        agent_id=None,
        agent_version=None,
        resolved_config=SECRETFUL,
        harness_config={"pipecat_version": "1.11.0"},
        queued_at=datetime.now(UTC),
        started_at=None,
        completed_at=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.unit
class TestTheRunResponse:
    def test_a_viewer_reading_a_run_gets_no_credentials(self) -> None:
        out = EvalRunResponse.model_validate(_row()).model_dump()
        assert _leaks(out) == []

    def test_it_masks_exactly_what_the_agent_endpoint_masks(self) -> None:
        """Equality with the masker, not a copy of its field list: a secret
        added to `sanitize_config` is covered here without touching this test."""
        out = EvalRunResponse.model_validate(_row())
        assert out.resolved_config == sanitize_config(SECRETFUL)
        assert out.target["agent"] == sanitize_config(SECRETFUL)

    def test_the_shape_survives_the_masking(self) -> None:
        out = EvalRunResponse.model_validate(_row())
        assert out.resolved_config["llm"]["model"] == "gpt-4o"
        assert "Authorization" in out.resolved_config["mcp_servers"][0]["headers"]
        assert out.target["type"] == "inline"


@pytest.mark.unit
class TestTheWebhookPayloads:
    def test_completed_carries_no_unmasked_secret(self) -> None:
        from turncall.evals import runner as runner_mod

        payload = runner_mod._completed_payload(_row())
        assert _leaks(payload) == []
        assert payload["resolved_config"] == sanitize_config(SECRETFUL)
        assert payload["target"]["agent"] == sanitize_config(SECRETFUL)

    def test_a_non_inline_target_is_untouched(self) -> None:
        from turncall.evals import runner as runner_mod

        agent_id = str(uuid4())
        payload = runner_mod._completed_payload(
            _row(target={"type": "agent", "agent_id": agent_id})
        )
        assert payload["target"] == {"type": "agent", "agent_id": agent_id}


@pytest.mark.unit
@pytest.mark.asyncio
class TestTheStoredSnapshot:
    async def test_the_run_row_stores_the_config_already_masked(self) -> None:
        """Nothing reads `resolved_config` back to execute anything, so the
        secret has no reason to sit in a JSONB column at all."""
        from turncall.evals import runner as runner_mod
        from turncall.evals.runner import ResolvedTarget

        run = _row(status="queued")
        start = AsyncMock()
        started_payloads: list[dict] = []
        target = ResolvedTarget(
            project_id=run.project_id,
            config=runner_mod.AgentConfig(),
            config_blob=SECRETFUL,
            agent_id=uuid4(),
        )

        async def _capture(session, *, payload, **_):
            started_payloads.append(payload)

        with (
            patch("turncall.storage.repositories.eval_repo.start_run", start),
            patch.object(runner_mod, "resolve_target", AsyncMock(return_value=target)),
            patch.object(runner_mod, "_dispatch_run_event", _capture),
        ):
            await runner_mod._plan_run(
                AsyncMock(),
                run,
                settings=SimpleNamespace(),
                session_factory=lambda: None,
            )

        assert start.await_args.kwargs["resolved_config"] == sanitize_config(SECRETFUL)
        assert _leaks(started_payloads) == []
