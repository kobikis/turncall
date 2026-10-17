"""Retention's rules, without a database (#316, ADR-0024).

The janitor against Postgres, and the reads after it, are
`tests/integration/test_retention_janitor.py` and `test_harness_retention.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.api.v1.schemas.calls import CallResponse
from turncall.api.v1.schemas.projects import ProjectRetention
from turncall.domain.enums import CallEventType
from turncall.domain.models import AgentConfig
from turncall.services import retention
from turncall.services.call_view import CallFacts, derived_fields
from turncall.services.retention import EVENTS, RECORDING, TRANSCRIPT, Retention

pytestmark = pytest.mark.unit

PROJECT = uuid.uuid4()


def _agent(
    name: str, version: int, state: str, override: dict[str, Any] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=PROJECT,
        name=name,
        version=version,
        state=state,
        config_blob={"retention": override} if override is not None else {},
    )


def test_unset_is_kept_for_good() -> None:
    assert Retention().empty
    assert Retention().as_payload() == {
        "recordings_days": None,
        "transcripts_days": None,
        "events_days": None,
    }
    assert retention.of_project(None) == Retention()


def test_an_agent_overrides_each_kind_it_sets() -> None:
    project = Retention(recordings_days=30, transcripts_days=30, events_days=None)
    agent = Retention(transcripts_days=2, events_days=90)
    assert agent.over(project) == Retention(
        recordings_days=30, transcripts_days=2, events_days=90
    )
    assert Retention().over(project) == project


def test_a_stored_block_a_later_build_refuses_is_no_override() -> None:
    assert retention.of_agent_config({"retention": {"weeks": 3}}) == Retention()
    assert retention.of_agent_config({}) == Retention()
    assert retention.of_agent_config(None) == Retention()
    assert retention.of_agent_config(
        {"retention": {"recordings_days": 7, "events_days": None}}
    ) == Retention(recordings_days=7)


def test_every_version_takes_its_names_live_version() -> None:
    """The published version's override applies to every version's calls; with
    none published, the newest's."""
    v1 = _agent("support", 1, "archived", {"transcripts_days": 365})
    v2 = _agent("support", 2, "published", {"transcripts_days": 30})
    v3 = _agent("support", 3, "draft", {"transcripts_days": 1})
    other = _agent("sales", 1, "draft", {"recordings_days": 5})
    other_v2 = _agent("sales", 2, "draft")

    overrides = retention._overrides([v1, v2, v3, other, other_v2])

    assert overrides[v1.id] == overrides[v2.id] == overrides[v3.id]
    assert overrides[v1.id] == Retention(transcripts_days=30)
    # sales v2 is the newest and sets nothing: neither version overrides.
    assert other.id not in overrides and other_v2.id not in overrides


def test_one_cutoff_per_distinct_retention() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    plan = retention._Plan(
        PROJECT,
        Retention(transcripts_days=30, events_days=90),
        {
            a: Retention(transcripts_days=2),
            b: Retention(transcripts_days=2, recordings_days=7),
            c: Retention(recordings_days=14),
        },
    )

    transcripts = plan.cutoffs(TRANSCRIPT)
    assert sorted((d, s.only, set(s.exclude)) for d, s in transcripts) == [
        (2, (a, b), set()),
        (30, None, {a, b}),
    ]
    # No project recording retention: only the agents that set one.
    assert sorted((d, s.only) for d, s in plan.cutoffs(RECORDING)) == [
        (7, (b,)),
        (14, (c,)),
    ]
    assert [(d, s.only, s.exclude) for d, s in plan.cutoffs(EVENTS)] == [(90, None, ())]
    assert retention._Plan(PROJECT, Retention()).cutoffs(EVENTS) == []


def test_the_transcript_is_the_conversation_and_what_was_made_from_it() -> None:
    assert retention.TRANSCRIPT_EVENTS == {
        CallEventType.TRANSCRIPT_PARTIAL,
        CallEventType.TRANSCRIPT_FINAL,
        CallEventType.TOOL_CALLED,
        CallEventType.TOOL_RESULT,
        CallEventType.CONTEXT_INJECTED,
    }


def test_the_project_setting_is_whole_days_from_one() -> None:
    assert ProjectRetention().model_dump() == {
        "recordings_days": None,
        "transcripts_days": None,
        "events_days": None,
    }
    assert ProjectRetention(transcripts_days=30).transcripts_days == 30
    for bad in ({"transcripts_days": 0}, {"events_days": 36501}, {"days": 3}):
        with pytest.raises(ValidationError):
            ProjectRetention(**bad)


def test_an_agent_config_carries_its_override() -> None:
    config = AgentConfigSchema(retention={"recordings_days": 7})
    blob = config.model_dump()
    assert blob["retention"] == {
        "recordings_days": 7,
        "transcripts_days": None,
        "events_days": None,
    }
    assert AgentConfig(**blob).retention.recordings_days == 7
    assert retention.of_agent_config(blob) == Retention(recordings_days=7)
    # Unset by default, so a config without it overrides nothing.
    assert retention.of_agent_config(AgentConfigSchema().model_dump()).empty
    with pytest.raises(ValidationError):
        AgentConfigSchema(retention={"recordings_days": 0})
    with pytest.raises(ValidationError):
        AgentConfigSchema(retention={"recording_days": 7})


def _call(**values: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{"status": "completed", "ended_at": datetime.now(UTC), **values}
    )


def test_the_read_keeps_how_the_call_ended_once_its_events_are_gone() -> None:
    """Frozen when the events expire: with them gone, inference would say
    `customer_ended_call` of a call the agent hung up."""
    gone = CallFacts()
    assert derived_fields(_call(), gone)["ended_reason"] == "customer_ended_call"
    frozen = _call(ended_reason="assistant_ended_call")
    assert derived_fields(frozen, gone)["ended_reason"] == "assistant_ended_call"


def test_the_call_read_says_what_expired() -> None:
    when = datetime(2026, 10, 1, tzinfo=UTC)
    row = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=PROJECT,
        provider="twilio",
        provider_call_sid=None,
        direction="inbound",
        from_number=None,
        to_number=None,
        active_agent_id=None,
        status="completed",
        started_at=None,
        ended_at=None,
        duration_ms=None,
        recording_status="completed",
        recording_url=None,
        metadata_json={},
        analysis_json=None,
        created_at=when,
        recording_expired_at=when,
        transcript_expired_at=None,
        events_expired_at=None,
    )
    read = CallResponse.from_row(row)
    assert read.recording_expired_at == when
    assert read.transcript_expired_at is None
