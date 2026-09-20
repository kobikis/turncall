"""The transport bridge, end to end, against a real LLM (#70, ADR-0018).

This is the one test that proves the claim the whole design rests on: that
swapping only the transport runs the agent's *real* pipeline. It builds a real
agent config, runs pipecat's scripted harness against it over a loopback
socket, and asserts on the verdict.

Text modality, so no STT or TTS service is *used* — but both are still
constructed by `create_pipeline`, which is exactly the path #63, #64 and #65
all broke in. A provider-name regression fails this test at pipeline build.

Live: needs OPENAI_API_KEY (the agent's LLM) and DEEPGRAM_API_KEY (constructed,
not called). Skipped without them.
"""

import pytest

from turncall.domain.enums import EvalKind, EvalModality
from turncall.domain.models import AgentConfig
from turncall.evals import scenario as scenario_mod
from turncall.evals.harness import run_iteration
from turncall.evals.runner import ResolvedTarget, map_script_result

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


def _target(
    project_id,
    prompt: str,
    first_message: str | None = None,
    tools: list | None = None,
):
    blob = {
        "system_prompt": prompt,
        "first_message": first_message,
        "llm": {"provider": "openai", "model": "gpt-4o-mini"},
        # Smart turn loads a local ONNX model and needs audio to do anything;
        # a text-mode run has none, so keep the build cheap.
        "smart_turn_detection": False,
        "tools": tools or [],
    }
    return ResolvedTarget(
        project_id=project_id,
        config=AgentConfig.model_validate(blob),
        config_blob=blob,
        agent_id=None,
    )


async def _run(definition: dict, target, session_factory) -> object:
    merged = scenario_mod.with_modality(definition, EvalModality.TEXT)
    parsed = scenario_mod.parse(merged, name="live-bridge")
    from turncall.config.settings import Settings

    return await run_iteration(
        parsed=parsed,
        kind=EvalKind.SCRIPTED,
        target=target,
        modality=EvalModality.TEXT,
        settings=Settings(),
        session_factory=session_factory,
        run_id=__import__("uuid").uuid4(),
    ), parsed


@pytest.fixture
def session_factory():
    """A session factory the eval path never actually uses.

    An eval has no `calls` row, so every call-scoped write is skipped — this
    exists only because `build_call_pipeline` loads KB attachments through it.
    """
    from turncall.config.settings import Settings
    from turncall.storage.database import create_engine, create_session_factory

    engine = create_engine(Settings().database)
    return create_session_factory(engine)


async def test_the_agent_answers_and_the_assertion_passes(
    openai_key: str, session_factory
) -> None:
    """The tracer bullet: a real pipeline, a real LLM, a scripted expectation."""
    from uuid import uuid4

    definition = {
        "turns": [
            {
                "user": "What is the capital of France? Answer with just the city.",
                "expect": [
                    {
                        "event": "llm_response",
                        "text_contains": "Paris",
                        "within_ms": 30000,
                    }
                ],
            }
        ]
    }
    target = _target(uuid4(), "You are a terse geography assistant.")
    result, parsed = await _run(definition, target, session_factory)

    assert result.passed, [str(f) for f in result.failures]
    outcome = map_script_result(result, iteration=1, parsed=parsed)
    assert outcome.passed is True
    assert outcome.entry["transcript"][0]["role"] == "user"
    assert any(m["role"] == "assistant" for m in outcome.entry["transcript"])


async def test_a_wrong_answer_fails_with_a_readable_reason(
    openai_key: str, session_factory
) -> None:
    """A failing run has to say what it wanted and what it got — a red cross
    with no reason is a suite nobody trusts."""
    from uuid import uuid4

    definition = {
        "turns": [
            {
                "user": "What is the capital of France? Answer with just the city.",
                "expect": [
                    {
                        "event": "llm_response",
                        "text_contains": "Ouagadougou",
                        "within_ms": 30000,
                    }
                ],
            }
        ]
    }
    target = _target(uuid4(), "You are a terse geography assistant.")
    result, parsed = await _run(definition, target, session_factory)

    assert not result.passed
    outcome = map_script_result(result, iteration=1, parsed=parsed)
    assert outcome.passed is False
    failure = outcome.entry["failures"][0]
    assert failure["kind"] == "text_mismatch"
    assert failure["reason"]


async def test_a_first_message_is_invisible_to_a_text_mode_eval(
    openai_key: str, session_factory
) -> None:
    """A known coverage hole, pinned here so a change to it is noticed.

    `first_message` goes out as a `TTSSpeakFrame`: it never passes through the
    LLM, so there is no `llm_response`, and text mode's `skip_tts` means the
    TTS emits nothing either. The harness therefore sees *no events at all* —
    a text-mode scenario cannot assert on the agent's greeting, which is one of
    the more obvious things someone will reach for first.

    Whether audio mode can see it is #72's to verify, along with the other two
    claims the design marks unverified. Until then the workaround is to leave
    `first_message` unset on an evaluated agent and let the LLM open the
    conversation, which a text-mode scenario *can* see.
    """
    from uuid import uuid4

    definition = {
        "turns": [
            {
                "user": None,
                "expect": [
                    {
                        "event": "llm_response",
                        "text_contains": "Acme",
                        "within_ms": 10000,
                    }
                ],
            }
        ]
    }
    target = _target(
        uuid4(),
        "You are a receptionist.",
        first_message="Thanks for calling Acme, how can I help?",
    )
    result, _parsed = await _run(definition, target, session_factory)

    assert not result.passed
    assert result.failures[0].kind == "timeout"
    assert result.events_seen == [], (
        "a first_message that starts producing events is a change worth noticing"
    )


# Discard port: the LLM's call still fires the `function_call` event, which is
# what the assertion is about, but nothing reachable receives the webhook. This
# is exactly the hazard #71 exists to remove -- an agent with a real
# webhook_url would have booked something here.
_NOWHERE = "http://127.0.0.1:9/never"

_BOOK_TOOL = {
    "name": "book_appointment",
    "description": "Book an appointment for the caller on a given day.",
    "parameters_schema": {
        "type": "object",
        "properties": {"day": {"type": "string"}},
        "required": ["day"],
    },
    "webhook_url": _NOWHERE,
    "timeout_seconds": 2,
    "max_retries": 0,
}


async def test_a_function_call_assertion_sees_the_tool_the_agent_called(
    openai_key: str, session_factory
) -> None:
    """User story 2: assert the agent calls a specific tool at a specific point,
    so a prompt change cannot silently break a workflow.

    The plumbing is pipecat's -- `required_report_level()` makes the harness ask
    the bot to report calls -- but it runs through *our* tool bridge, so this is
    the check that the two actually meet.
    """
    from uuid import uuid4

    definition = {
        "turns": [
            {
                "user": "Please book me an appointment for Friday.",
                "expect": [
                    {
                        "event": "function_call",
                        "name": "book_appointment",
                        "within_ms": 30000,
                    }
                ],
            }
        ]
    }
    target = _target(
        uuid4(),
        "You are a receptionist. Use the book_appointment tool when asked to book.",
        tools=[_BOOK_TOOL],
    )
    result, _parsed = await _run(definition, target, session_factory)
    assert result.passed, [str(f) for f in result.failures]


async def test_a_function_call_assertion_fails_when_the_tool_is_not_called(
    openai_key: str, session_factory
) -> None:
    """The half that matters for story 2: the assertion has to be capable of
    failing, or it protects nothing."""
    from uuid import uuid4

    definition = {
        "turns": [
            {
                "user": "What are your opening hours?",
                "expect": [
                    {
                        "event": "function_call",
                        "name": "book_appointment",
                        "within_ms": 15000,
                    }
                ],
            }
        ]
    }
    target = _target(
        uuid4(),
        "You are a receptionist. We are open 9 to 5. Only use the "
        "book_appointment tool if the caller explicitly asks to book.",
        tools=[_BOOK_TOOL],
    )
    result, parsed = await _run(definition, target, session_factory)

    assert not result.passed
    outcome = map_script_result(result, iteration=1, parsed=parsed)
    assert outcome.entry["failures"][0]["kind"] in (
        "missing_function_call",
        "timeout",
    )
