"""The derived half of a call's representation (#175).

`ended_reason`, `agent_name` and `agent_version` are worked out, never stored,
and the call read and `call.ended` both take them from `derived_fields` — so the
two views of one call cannot disagree about why it ended.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from turncall.services.call_view import CallFacts, derived_fields

pytestmark = pytest.mark.unit

ENDED = datetime(2026, 9, 30, 12, 42, tzinfo=UTC)


def _call(status="completed", ended_at=ENDED):
    return SimpleNamespace(status=status, ended_at=ended_at)


def test_completed_call_ended_by_customer():
    view = derived_fields(_call(), CallFacts(event_types=frozenset({"call.ended"})))
    assert view["ended_reason"] == "customer_ended_call"


def test_assistant_ended_follows_the_facts():
    view = derived_fields(_call(), CallFacts(assistant_ended=True))
    assert view["ended_reason"] == "assistant_ended_call"


def test_voicemail_event_wins():
    facts = CallFacts(event_types=frozenset({"voicemail.detected"}))
    assert derived_fields(_call(), facts)["ended_reason"] == "voicemail"


def test_live_call_has_no_ended_reason():
    # "unknown" on a call that is still talking would read as a verdict.
    view = derived_fields(_call(status="in_progress", ended_at=None), CallFacts())
    assert view["ended_reason"] is None


def test_ended_but_still_marked_handed_off_gets_a_reason():
    # ended_at is the signal the call is over, whatever the status says.
    view = derived_fields(_call(status="handed_off"), CallFacts())
    assert view["ended_reason"] == "unknown"


def test_agent_name_and_version():
    view = derived_fields(_call(), CallFacts(agent_name="support", agent_version=3))
    assert (view["agent_name"], view["agent_version"]) == ("support", 3)


def test_inline_or_deleted_agent_is_null_not_invented():
    view = derived_fields(_call(), CallFacts())
    assert view["agent_name"] is None and view["agent_version"] is None


def _turn(n, user_to_bot, **stages):
    return {
        "turn": n,
        "user_to_bot_ms": user_to_bot,
        "stages": {k: {"ttfb_ms": v, "service": k.upper()} for k, v in stages.items()},
    }


def test_latency_summary_percentiles_and_slowest_turn():
    turns = tuple(
        _turn(i + 1, 800 + i * 100, stt=100 + i, llm=300 + i * 10, tts=200)
        for i in range(10)
    )
    summary = derived_fields(_call(), CallFacts(turn_metrics=turns))["latency_summary"]
    assert summary["turns"] == 10
    assert summary["user_to_bot"] == {"p50_ms": 1200, "p95_ms": 1700, "count": 10}
    assert summary["stages"]["llm"] == {"p50_ms": 340, "p95_ms": 390, "count": 10}
    assert summary["stages"]["tts"]["p95_ms"] == 200
    assert summary["slowest_turn"] == {"turn": 10, "user_to_bot_ms": 1700}


def test_latency_summary_omits_a_stage_no_turn_had():
    # An S2S call reports only the model's stage.
    turns = (_turn(1, 700, llm=400), _turn(2, 900, llm=500))
    summary = derived_fields(_call(), CallFacts(turn_metrics=turns))["latency_summary"]
    assert set(summary["stages"]) == {"llm"}


def test_no_turns_is_null_not_zeros():
    assert derived_fields(_call(), CallFacts())["latency_summary"] is None


# --- skills (#425) -----------------------------------------------------------

_RESOLVED = (
    {"id": "a", "skill": "verify_identity", "version": 2},
    {"id": "b", "skill": "explain_bill", "version": 1},
)


def test_every_resolved_skill_is_listed_loaded_or_not():
    facts = CallFacts(
        resolved_skills=_RESOLVED,
        skill_loads=({"skill": "verify_identity", "version": 2},),
    )
    assert derived_fields(_call(), facts)["skills"] == [
        {"skill": "verify_identity", "version": 2, "loaded": True},
        {"skill": "explain_bill", "version": 1, "loaded": False},
    ]


def test_a_load_marks_only_the_version_that_ran():
    """#427: after a handoff the call may hold two versions of one skill, the
    first agent's and the target's. A load is matched by name and version."""
    facts = CallFacts(
        resolved_skills=(
            {"id": "a", "skill": "explain_bill", "version": 1},
            {"id": "c", "skill": "explain_bill", "version": 2},
        ),
        skill_loads=({"skill": "explain_bill", "version": 2},),
    )
    assert [s["loaded"] for s in derived_fields(_call(), facts)["skills"]] == [
        False,
        True,
    ]


def test_a_call_with_no_skills_lists_none():
    assert derived_fields(_call(), CallFacts())["skills"] == []


def test_loaded_is_unknown_once_the_events_expired():
    call = SimpleNamespace(status="completed", ended_at=ENDED, events_expired_at=ENDED)
    facts = CallFacts(resolved_skills=_RESOLVED)
    assert [s["loaded"] for s in derived_fields(call, facts)["skills"]] == [
        None,
        None,
    ]


def test_the_stored_resolution_is_read_defensively():
    from turncall.services.call_view import (
        RESOLVED_SKILLS,
        public_metadata,
        resolved_skills,
    )

    metadata = {
        RESOLVED_SKILLS: [*_RESOLVED, "junk", {"skill": "x"}],
        "template_variables": {"a": "b"},
    }
    assert resolved_skills(metadata) == _RESOLVED
    assert resolved_skills({RESOLVED_SKILLS: "junk"}) == ()
    # Shown as `skills`, never in the metadata too.
    assert public_metadata(metadata) == {"template_variables": {"a": "b"}}
