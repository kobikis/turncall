"""The scenario vocabulary `evals-design.md` §3 documents, asserted field by field.

A definition is stored verbatim and parsed by pipecat, so every field pipecat's
schema has is reachable from the API for free. The failure mode of documenting
that is silent: pipecat's parser **ignores keys it does not know**, so an
example in the docs can stop meaning anything without anything going red —
which is exactly what happened to `matches:`, named in this project's own docs
as part of the vocabulary while the parser dropped it and every expectation
resting on it asserted nothing.

So parsing is not the assertion here. Each case parses a documented example and
then reads the **value** back off pipecat's dataclass: a rename or a removal in
a pipecat bump fails here, naming the field, instead of quietly turning a
documented feature into a no-op.

This is about the vocabulary reaching the parser.
`tests/unit/test_pipecat_parser_contract.py` guards the parser surface itself,
and §9.7 is why none of it reports a verdict yet.
"""

import pytest

from turncall.evals.scenario import parse

pytestmark = pytest.mark.unit


def _script(definition: dict):
    return parse(definition, name="vocabulary")


def test_within_ms_is_a_per_turn_budget() -> None:
    """§3 "A latency budget"."""
    scenario = _script(
        {
            "turns": [
                {
                    "user": "What are your hours?",
                    "expect": [
                        {"event": "llm_started", "within_ms": 2000},
                        {"event": "llm_response", "text_contains": "nine"},
                    ],
                }
            ]
        }
    )
    first, second = scenario.turns[0].expect
    assert first.within_ms == 2000
    # Unset, not zero: pipecat applies its own 60s default downstream, and a 0
    # here would read as "no time at all".
    assert second.within_ms is None
    assert second.text_contains == "nine"


def test_send_after_anchors_a_barge_in() -> None:
    """§3 "Barge-in" — the assertion `interruption_enabled` has never had."""
    scenario = _script(
        {
            "turns": [
                {
                    "user": "Tell me a long, detailed story about the history of Paris.",
                    "expect": [{"event": "llm_started"}],
                },
                {
                    "user": "Actually, never mind. What is the capital of Japan?",
                    "send_after": {"event": "llm_started", "delay_ms": 2000},
                    "expect": [
                        {"event": "bot_interrupted"},
                        {
                            "event": "llm_response",
                            "text_contains": "Tokyo",
                            "text_excludes": "Paris",
                        },
                    ],
                },
            ]
        }
    )
    interrupting = scenario.turns[1]
    assert interrupting.send_after is not None
    assert interrupting.send_after.event == "llm_started"
    assert interrupting.send_after.delay_ms == 2000
    assert interrupting.expect[0].event == "bot_interrupted"
    assert interrupting.expect[1].text_excludes == "Paris"


def test_a_bare_delay_needs_no_event() -> None:
    """A `send_after` with only a delay is a plain wait from the previous send."""
    scenario = _script(
        {"turns": [{"user": "one"}, {"user": "two", "send_after": {"delay_ms": 500}}]}
    )
    send_after = scenario.turns[1].send_after
    assert send_after.event is None
    assert send_after.delay_ms == 500


def test_absent_asserts_a_quiet_window() -> None:
    """§3 "Nothing should arrive" — the smart-turn check nothing else can make."""
    scenario = _script(
        {
            "turns": [
                {
                    "user": "I'd go to Japan because",
                    "expect": [
                        {"event": "llm_response", "absent": True, "within_ms": 3000}
                    ],
                }
            ]
        }
    )
    expectation = scenario.turns[0].expect[0]
    assert expectation.absent is True
    assert expectation.within_ms == 3000


def test_stop_on_failure_can_be_turned_off() -> None:
    """§3 "Score every turn" — independent turns, e.g. intent classification."""
    scenario = _script(
        {
            "stop_on_failure": False,
            "turns": [
                {
                    "user": "Book me a flight to Tokyo.",
                    "expect": [
                        {
                            "event": "function_call",
                            "within_ms": 15000,
                            "calls": [{"name": "book_flight"}],
                        }
                    ],
                }
            ],
        }
    )
    assert scenario.stop_on_failure is False
    # The default is the opposite, and a scenario that silently scored every
    # turn would report failures from turns taken after the conversation broke.
    assert _script({"turns": [{"user": "hi"}]}).stop_on_failure is True


def test_function_call_stopped_carries_cancelled() -> None:
    """§3 "A cancelled tool" — the assertion for `execution_mode`."""
    scenario = _script(
        {
            "turns": [
                {
                    "user": "Actually, cancel that.",
                    "expect": [
                        {
                            "event": "function_call_stopped",
                            "calls": [
                                {"name": "lookup_order", "args": {"cancelled": True}}
                            ],
                        }
                    ],
                }
            ]
        }
    )
    call = scenario.turns[0].expect[0].calls[0]
    assert call.name == "lookup_order"
    assert call.args == {"cancelled": True}


def test_a_function_call_takes_eval_as_well_as_args() -> None:
    """§3 "Judging a call the model phrases itself"."""
    scenario = _script(
        {
            "turns": [
                {
                    "user": "My thermostat is broken. I'm Jennifer Smith.",
                    "expect": [
                        {
                            "event": "function_call",
                            "calls": [{"name": "submit_ticket"}],
                            "eval": "a ticket about a broken thermostat, for Jennifer Smith",
                        }
                    ],
                }
            ]
        }
    )
    expectation = scenario.turns[0].expect[0]
    assert expectation.calls[0].name == "submit_ticket"
    assert expectation.eval.startswith("a ticket about")


def test_context_is_parsed_as_the_whole_context() -> None:
    """§3 "Seeding history" — and the trap: this REPLACES the agent's prompt.

    Asserted rather than described because the day pipecat makes `context:`
    additive is the day the warning in the docs becomes wrong, and a scenario
    written around the old behaviour starts testing a different agent.
    """
    scenario = _script(
        {
            "context": [
                {"role": "system", "content": "You are Acme's receptionist."},
                {"role": "assistant", "content": "Thanks for calling Acme."},
            ],
            "turns": [{"user": "I'm calling back about ticket 4127."}],
        }
    )
    assert [m["role"] for m in scenario.context] == ["system", "assistant"]


def test_simulation_metrics_judged_and_measured() -> None:
    """§3 "Simulation thresholds" — including the empty `calls:` list."""
    scenario = parse(
        {
            "persona": "Jamie, booking dinner for two at 6 PM.",
            "goal": "Book a table for two at 6 PM, then end the call.",
            "success": "the bot confirmed a reservation for two at 6 PM",
            "metrics": [
                {
                    "name": "politeness",
                    "criterion": "the reply is courteous, never curt",
                    "min_score": 1,
                },
                {"measure": "words", "max_value": 60},
                {"measure": "latency", "max_value": 5},
                {
                    "measure": "function_calls",
                    "calls": [{"name": "book_table", "args": {"party_size": 2}}],
                },
            ],
            "max_turns": 8,
            "max_duration_s": 120,
            "max_silence_s": 30,
        },
        name="vocabulary",
    )
    judged, words, latency, calls = scenario.metrics
    assert judged.criterion and judged.min_score == 1
    # A measured metric has no criterion and never reaches the judge.
    assert words.measure == "words" and words.max_value == 60
    assert words.criterion is None
    assert latency.measure == "latency" and latency.max_value == 5
    assert calls.measure == "function_calls"
    assert calls.calls[0].args == {"party_size": 2}
    assert (scenario.max_turns, scenario.max_duration_s, scenario.max_silence_s) == (
        8,
        120,
        30,
    )


def test_an_empty_calls_list_survives_the_parse() -> None:
    """`calls: []` is the assertion for an agent that must call nothing.

    It is also the one most likely to be swallowed as falsy on the way through,
    which would turn "call nothing" into "no assertion at all" — the loudest
    possible silent failure, since the scenario exists to prove a caller was
    turned down.
    """
    scenario = parse(
        {
            "persona": "A caller asking for something the agent must refuse.",
            "goal": "Try to book without giving a name.",
            "success": "the bot declined and booked nothing",
            "metrics": [{"measure": "function_calls", "calls": []}],
        },
        name="vocabulary",
    )
    assert scenario.metrics[0].calls == []


def test_matches_is_still_not_a_field() -> None:
    """The reason this file exists: a dropped key is a silent no-op.

    `matches:` was documented here as part of the vocabulary. Pipecat has no
    such field, so the parser discards it and the expectation asserts only that
    an event arrived. If a pipecat release ever adds it, this fails and the
    docs (and `_ASSERTING_FIELDS`) get to change on purpose.
    """
    scenario = _script(
        {
            "turns": [
                {
                    "user": "What are your hours?",
                    "expect": [{"event": "llm_response", "matches": "nine to five"}],
                }
            ]
        }
    )
    expectation = scenario.turns[0].expect[0]
    assert not hasattr(expectation, "matches")
    assert expectation.text_contains is None and expectation.eval is None
