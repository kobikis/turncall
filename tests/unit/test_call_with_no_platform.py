"""A call with no database behind it — the website demo (`turncall.demo`).

`CallContext.records` is what every call-scoped side effect asks: false for an
eval (no `calls` row) and for a call with no database at all. Such a call
still has to run what needs no platform: its keys come from the environment,
and `end_call` says goodbye and hangs up with nothing to record.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from turncall.orchestrator.pipeline_factory import CallContext

pytestmark = pytest.mark.unit


def _context(**overrides) -> CallContext:
    return CallContext(
        **{
            "call_id": uuid4(),
            "project_id": uuid4(),
            "agent_id": uuid4(),
            "call_sid": "webrtc",
            "stream_sid": "webrtc",
            "session_factory": object(),
            **overrides,
        }
    )


class TestRecords:
    def test_a_call_records(self) -> None:
        assert _context().records is True

    def test_an_eval_does_not(self) -> None:
        context = _context(eval_run_id=uuid4())
        assert context.has_database is True
        assert context.records is False

    def test_a_call_with_no_database_does_not(self) -> None:
        context = _context(session_factory=None)
        assert context.has_database is False
        assert context.records is False
        # Not an eval: the hangup gate, the missing-key check and the rest of
        # what an eval turns off stay on.
        assert context.is_eval is False


class TestBuiltins:
    async def test_end_call_says_goodbye_with_nothing_to_record(self) -> None:
        from turncall.orchestrator.tool_bridge import _ends_the_call, _execute_builtin
        from turncall.services.call_control import ASSISTANT_END_MESSAGE

        result = await _execute_builtin("end_call", {}, _context(session_factory=None))
        assert json.loads(result)["message"] == ASSISTANT_END_MESSAGE
        assert _ends_the_call(result)

    @pytest.mark.parametrize(
        "name", ["transfer_call", "handoff_to_agent", "send_dtmf", "do_not_call"]
    )
    async def test_the_rest_are_refused(self, name: str) -> None:
        from turncall.orchestrator.tool_bridge import _ends_the_call, _execute_builtin

        result = await _execute_builtin(name, {}, _context(session_factory=None))
        assert json.loads(result)["success"] is False
        assert not _ends_the_call(result)


async def test_its_keys_come_from_the_environment(monkeypatch) -> None:
    from turncall.config.settings import Settings
    from turncall.orchestrator.pipeline_builder import load_call_credentials

    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-from-env")
    credentials = await load_call_credentials(None, uuid4(), Settings())
    assert credentials.api_key("deepgram") == "dg-from-env"
    assert dict(credentials.project) == {}
