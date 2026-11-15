"""The pipeline build takes only an accepted agent (#481, #474 story 24).

Agent acceptance is the one place that decides whether a config can run, so
the build's entry takes its answer, an `AcceptedAgent`, and nothing else: a
new entry point that skips acceptance has no config to hand the build. The
build's own copies of the provider and procedure rules are gone with it.

These are structural: the entry's parameters, every caller in the source
passing an accepted agent, and `AcceptedAgent` built by acceptance alone. What
a caller may still do to an accepted agent (render the call's variables, pin
skills, cap the duration) is a method on it that changes nothing a rule reads.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

import turncall
from tests.accepted import accepted
from turncall.domain.models import SkillRef
from turncall.orchestrator import pipeline_builder
from turncall.services.agent_acceptance import AcceptedAgent

pytestmark = pytest.mark.unit

SRC = Path(turncall.__file__).parent
BUILD_ENTRIES = ("build_call_pipeline",)


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


@pytest.mark.parametrize("entry", BUILD_ENTRIES)
def test_the_build_entry_takes_an_accepted_agent_and_no_config(entry: str) -> None:
    params = inspect.signature(getattr(pipeline_builder, entry)).parameters

    assert "config" not in params
    assert params["agent"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["agent"].annotation in ("AcceptedAgent", AcceptedAgent)


def test_a_call_to_run_carries_an_accepted_agent_and_no_config() -> None:
    """What every voice edge hands `run_call` (#491), which builds on it."""
    from turncall.orchestrator.call_runner import CallToRun

    fields = {f.name: f.type for f in dataclasses.fields(CallToRun)}
    assert "config" not in fields
    assert fields["agent"] in ("AcceptedAgent", AcceptedAgent)


def test_every_caller_of_the_build_passes_an_accepted_agent() -> None:
    """By keyword, `agent=`: a `config=` would be refused by the signature at
    runtime, but only on the path that reaches it."""
    calls: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in BUILD_ENTRIES:
                continue
            where = f"{path.relative_to(SRC)}:{node.lineno}"
            keywords = {k.arg for k in node.keywords}
            assert "agent" in keywords and "config" not in keywords, where
            calls.append(where)
    # media stream, run_call (WebRTC, WhatsApp and the demo) and the eval
    # harness.
    assert len(calls) == 3, calls


def test_only_agent_acceptance_builds_an_accepted_agent() -> None:
    """Anything else making one would be a config nothing checked."""
    builders = set()
    for path in _source_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "AcceptedAgent"
            ):
                builders.add(path.relative_to(SRC).as_posix())
    assert builders == {"services/agent_acceptance.py"}


ACCEPTED_FIELDS = frozenset(f.name for f in dataclasses.fields(AcceptedAgent))


def _replaces_accepted_fields(source: str) -> list[int]:
    """The lines of `source` that copy a dataclass with an `AcceptedAgent`
    field changed — `replace(...)`, `dataclasses.replace(...)`,
    `copy.replace(...)`, `x.__replace__(...)` — or with changes nobody can
    read (`**changes`). Without the types, a field name is the tell: no
    other replace in the source changes one (rename it if a new one does)."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        bare = isinstance(func, ast.Name) and func.id == "replace"
        qualified = isinstance(func, ast.Attribute) and (
            func.attr == "__replace__"
            or (
                func.attr == "replace"
                and isinstance(func.value, ast.Name)
                and func.value.id in ("dataclasses", "copy")
            )
        )
        if not (bare or qualified):
            continue
        changed = {k.arg for k in node.keywords}
        if None in changed or changed & ACCEPTED_FIELDS:
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize(
    "source",
    [
        "replace(agent, config=other)",
        "dataclasses.replace(agent, agent_id=None)",
        "copy.replace(agent, warnings=())",
        "agent.__replace__(stored={})",
        "replace(agent, **changes)",
    ],
)
def test_the_guard_sees_a_replace_that_changes_an_accepted_field(source: str) -> None:
    assert _replaces_accepted_fields(source) == [1]


@pytest.mark.parametrize(
    "source",
    ["replace(context, skills=s)", "text.replace('a', 'b')", "replace(agent)"],
)
def test_the_guard_leaves_other_replaces_alone(source: str) -> None:
    assert _replaces_accepted_fields(source) == []


def test_only_agent_acceptance_replaces_an_accepted_agents_fields() -> None:
    """A copy with a field changed is a new config nothing checked, as much
    as one built by hand."""
    found = []
    for path in _source_files():
        where = path.relative_to(SRC).as_posix()
        if where == "services/agent_acceptance.py":
            continue
        found += [
            f"{where}:{line}" for line in _replaces_accepted_fields(path.read_text())
        ]
    assert found == []


def test_the_build_holds_no_copy_of_a_rule() -> None:
    """The rules live in agent acceptance; the build reads its answer. The
    orchestrator defines some of them (no pipecat), and calls none."""
    rules = {
        "llm_refusal",
        "tts_refusal",
        "refusal",  # grok_voice's and eleven_realtime's
        "duration_refusal",
        "gateway_refusal",
        "mixes_flux",
        "procedures_refusal",
    }
    found = set()
    for path in (SRC / "orchestrator").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in rules:
                found.add(f"{path.relative_to(SRC)}:{node.lineno} {name}")
    assert found == set()


# --- what a caller may still do to an accepted agent --------------------------


def test_rendering_applies_the_calls_variables_and_knowledge_context() -> None:
    agent = accepted(
        {"system_prompt": "Hi {{name}}.", "first_message": "Hello {{name}}."}
    )

    rendered = agent.rendered({"name": "Jane"}, knowledge_context="Ticket #4.")

    assert isinstance(rendered, AcceptedAgent)
    assert rendered.config.system_prompt == "Ticket #4.\n\nHi Jane."
    assert rendered.config.first_message == "Hello Jane."
    assert agent.config.system_prompt == "Hi {{name}}."  # a new one, not changed


def test_rendering_with_nothing_to_apply_is_the_same_agent() -> None:
    agent = accepted({"system_prompt": "Hi."})

    assert agent.rendered({}, knowledge_context=None) == agent


def test_rendering_keeps_the_agent_id_and_warnings() -> None:
    agent = accepted({"system_prompt": "Hi {{name}}.", "voice_style": "warm"})

    rendered = agent.rendered({"name": "Jane"})

    assert (rendered.agent_id, rendered.warnings) == (agent.agent_id, agent.warnings)


def test_pinning_skills_replaces_the_list() -> None:
    agent = accepted({"skills": [{"skill": "billing"}]})
    pinned = [
        SkillRef(skill="billing", version_id="00000000-0000-0000-0000-00000000000b")
    ]

    assert agent.with_skills(pinned).config.skills == pinned
    assert agent.with_skills([]).config.skills == []


@pytest.mark.parametrize(("cap", "expected"), [(60, 60), (3600, 600)])
def test_capping_lowers_the_duration_only(cap: int, expected: int) -> None:
    agent = accepted({"max_call_duration_seconds": 600})

    assert agent.capped(cap).config.max_call_duration_seconds == expected
