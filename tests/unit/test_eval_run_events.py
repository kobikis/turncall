"""An eval run announces itself: `eval.run.started` and `eval.run.completed`.

Two events, not a scatter: the completed one is comprehensive and carries the
whole result, the way `call.ended` does (ADR-0006). `analysis.completed` is the
counter-example — reserved in the enum, never dispatched, and a standing source
of "why does nothing arrive".

The run's identity rides in the **envelope** beside `call_id` and `session_id`,
never in the payload (ADR-0007), and is null on every event that is not about a
run (#76).
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from turncall.domain.enums import CallEventType
from turncall.events import dispatcher
from turncall.events.webhook_delivery import DeliveryResult, WebhookEvent

pytestmark = pytest.mark.unit


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    async def fake_subscribers(session, project_id, event_type):
        return [("https://example.test/hook", "secret")]

    async def fake_deliver(event: WebhookEvent, subscribers):
        captured["event"] = event
        return [DeliveryResult(success=True, status_code=200, attempts=1)]

    monkeypatch.setattr(dispatcher, "get_active_subscribers", fake_subscribers)
    monkeypatch.setattr(dispatcher, "deliver_to_subscribers", fake_deliver)
    return captured


async def _drain() -> None:
    import asyncio

    while dispatcher._BG_TASKS:
        await asyncio.gather(*list(dispatcher._BG_TASKS))


@pytest.mark.asyncio
async def test_the_run_id_rides_in_the_envelope(monkeypatch) -> None:
    run_id = uuid4()
    captured = _capture(monkeypatch)

    await dispatcher.dispatch_event(
        AsyncMock(),
        project_id=uuid4(),
        event_type=CallEventType.EVAL_RUN_STARTED,
        payload={"scenario_name": "greets"},
        eval_run_id=run_id,
    )
    await _drain()

    event = captured["event"]
    assert event.eval_run_id == run_id
    assert event.call_id is None and event.session_id is None
    # Identity in the envelope, event-specific data in the payload (ADR-0007).
    assert "eval_run_id" not in event.payload


@pytest.mark.asyncio
async def test_a_non_eval_event_carries_a_null_run_id(monkeypatch) -> None:
    """Additive and nullable exactly as `session_id` was: no subscriber
    watching calls sees anything change."""
    captured = _capture(monkeypatch)

    async def fake_get_call(session, call_id, **kw):
        return SimpleNamespace(active_agent_id=None)

    monkeypatch.setattr(dispatcher.call_repo, "get_call_by_id", fake_get_call)
    await dispatcher.dispatch_event(
        AsyncMock(),
        project_id=uuid4(),
        event_type=CallEventType.CALL_ENDED,
        payload={},
        call_id=uuid4(),
    )
    await _drain()

    assert captured["event"].eval_run_id is None


@pytest.mark.asyncio
async def test_the_signed_body_carries_the_field(monkeypatch) -> None:
    """The envelope is what gets signed, so a field missing from the body is
    missing from the contract however right the dataclass looks."""
    from turncall.events import webhook_delivery

    run_id = uuid4()
    sent: dict = {}

    class _Response:
        status_code = 200

    async def fake_post(url, content=None, headers=None, timeout=None):
        sent["body"] = content
        sent["headers"] = headers
        return _Response()

    monkeypatch.setattr(
        webhook_delivery, "get_http_client", lambda: SimpleNamespace(post=fake_post)
    )
    result = await webhook_delivery.deliver_webhook(
        "https://example.test/hook",
        WebhookEvent(
            event_type="eval.run.completed",
            payload={"status": "passed"},
            project_id=uuid4(),
            eval_run_id=run_id,
            event_id="stable-across-retries",
        ),
        "secret",
    )

    assert result.success
    body = json.loads(sent["body"])
    assert body["eval_run_id"] == str(run_id)
    assert body["call_id"] is None and body["session_id"] is None
    assert body["event_id"] == "stable-across-retries"
    assert sent["headers"]["X-TurnCall-Signature"]
    assert sent["headers"]["X-TurnCall-Event"] == "eval.run.completed"


class TestTheRunnerDispatches:
    """The runner's two dispatch sites, with the repository faked."""

    @staticmethod
    def _row(**over):
        base = dict(
            id=uuid4(),
            project_id=uuid4(),
            status="queued",
            modality="text",
            kind="script",
            iterations=1,
            scenario_name="greets",
            scenario_id=uuid4(),
            batch_id=uuid4(),
            target={"type": "agent", "agent_id": str(uuid4())},
            resolved_scenario={"definition": {"turns": [{"user": "hi", "expect": []}]}},
            passed_count=1,
            failed_count=0,
            error=None,
            results=[
                {"iteration": 1, "passed": True, "transcript": [], "failures": []}
            ],
            agent_id=uuid4(),
            agent_version=3,
            resolved_config={"system_prompt": "hi"},
            harness_config={"pipecat_version": "1.11.0"},
            queued_at=None,
            started_at=None,
            completed_at=None,
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_a_completed_payload_carries_the_whole_result(self) -> None:
        """Comprehensive by design: a subscriber that gets this never has to
        call back to find out what happened."""
        from turncall.evals import runner as runner_mod

        payload = runner_mod._completed_payload(
            self._row(status="failed", passed_count=2, failed_count=1)
        )
        assert payload["status"] == "failed"
        assert (payload["passed_count"], payload["failed_count"]) == (2, 1)
        assert payload["results"][0]["iteration"] == 1
        # The three snapshots, so a result stays interpretable later.
        assert payload["resolved_config"] == {"system_prompt": "hi"}
        assert payload["resolved_scenario"]["definition"]
        assert payload["harness_config"]["pipecat_version"]
        assert payload["agent_version"] == 3

    def test_an_inline_targets_agent_id_is_null_not_invented(self) -> None:
        """ADR-0017: null is the honest answer to "which stored agent was
        this", and the zero-UUID sentinel must never reach an event."""
        from turncall.evals import runner as runner_mod

        payload = runner_mod._completed_payload(
            self._row(agent_id=None, agent_version=None, target={"type": "inline"})
        )
        assert payload["agent_id"] is None
        assert payload["agent_version"] is None

    def test_an_errored_run_still_reports_a_status(self) -> None:
        """A run that was accepted and then vanished is worse than one that
        failed: a subscriber waiting on a terminal event would wait forever."""
        from turncall.evals import runner as runner_mod

        payload = runner_mod._completed_payload(
            self._row(
                status="errored",
                passed_count=0,
                failed_count=0,
                results=[],
                error="agent not found in this project",
            )
        )
        assert payload["status"] == "errored"
        assert payload["error"] == "agent not found in this project"
