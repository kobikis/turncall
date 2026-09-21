"""The pipecat surface scenario validation stands on (#100).

`evals/scenario.py` parses a stored definition with `pipecat.evals.script.
_parse_script` and `pipecat.evals.simulation._parse_simulation`. Both are
underscore-private and carry no compatibility promise, and they are load-bearing
for **all** validation: creating a scenario, updating one, and `_parse_for_run`
on every queued run. A rename in a pipecat minor would have the worker erroring
every run in the queue with a parse failure that has nothing to do with any
scenario.

Storing the definition verbatim and validating by round-trip is the right
call — the schema is pipecat's and mirroring it here would be standing
migration debt. This file is the cheap insurance: a pipecat bump that moves the
surface is a red build here, naming the version, rather than a production parse
failure.
"""

from importlib.metadata import version

import pytest

from turncall.domain.enums import EvalKind
from turncall.evals.scenario import SCHEMA_VERSION, ScenarioError, kind_of, parse

pytestmark = pytest.mark.unit

PIPECAT = version("pipecat-ai")
WHY = (
    f"TurnCall validates scenarios against {SCHEMA_VERSION}; pipecat {PIPECAT} is "
    "installed. If this failed after an upgrade, that upgrade moved the parser "
    "surface and every scenario create, update and queued run goes with it (#100)."
)

SCRIPTED = {
    "turns": [
        {
            "user": "what are your hours?",
            "expect": [{"event": "llm_response", "text_contains": "nine"}],
        }
    ]
}
# Top-level strings, not a nested block: pipecat's simulation parser reads
# `persona:`, `goal:` and `success:` off the mapping itself.
SIMULATION = {
    "persona": "a caller who wants a table for two",
    "goal": "book a table",
    "success": "the agent books a table for two",
}


def test_the_private_parsers_are_still_where_we_import_them_from() -> None:
    from turncall.evals.scenario import _parsers

    parsers = _parsers()
    assert set(parsers) == {EvalKind.SCRIPTED, EvalKind.SIMULATION}, WHY
    assert all(callable(parser) for parser in parsers.values()), WHY


def test_a_scripted_definition_still_parses() -> None:
    parsed = parse(SCRIPTED, name="hours")
    assert kind_of(SCRIPTED) is EvalKind.SCRIPTED, WHY
    assert parsed.turns[0].expect[0].text_contains == "nine", WHY


def test_a_simulation_definition_still_parses() -> None:
    parsed = parse(SIMULATION, name="booking")
    assert kind_of(SIMULATION) is EvalKind.SIMULATION, WHY
    assert parsed.persona, WHY


def test_the_fields_a_run_reads_off_a_parsed_scenario_are_still_there() -> None:
    """Not just "it parsed": the runner and the result mappers read these, so a
    rename that leaves parsing intact still breaks a run."""
    parsed = parse(SCRIPTED, name="hours")
    for attribute in ("name", "turns", "judge"):
        assert hasattr(parsed, attribute), f"{attribute} is gone. {WHY}"
    expectation = parsed.turns[0].expect[0]
    for attribute in ("event", "text_contains", "eval", "calls"):
        assert hasattr(expectation, attribute), f"{attribute} is gone. {WHY}"


def test_a_definition_pipecat_rejects_is_still_our_error_type() -> None:
    """The boundary converts pipecat's exception, whatever it is, into one the
    API knows how to turn into a 400."""
    with pytest.raises(ScenarioError):
        parse({"turns": [{"user": "hi", "expect": [{"event": 17}]}]}, name="bad")


def test_a_mapping_of_neither_kind_is_refused_before_pipecat_sees_it() -> None:
    with pytest.raises(ScenarioError, match="turns"):
        parse({"name": "neither"}, name="neither")
