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
        {"skill": "verify_identity", "version": 2, "pinned": False, "loaded": True},
        {"skill": "explain_bill", "version": 1, "pinned": False, "loaded": False},
    ]


def test_pinned_reads_from_the_stored_entry_and_defaults_to_false():
    """#426: an entry stored before pins existed has no `pinned`, and was a
    live-following skill."""
    facts = CallFacts(
        resolved_skills=(
            {"id": "a", "skill": "verify_identity", "version": 1, "pinned": True},
            {"id": "b", "skill": "explain_bill", "version": 1},
            {"id": "c", "skill": "promise", "version": 3, "pinned": "yes"},
        ),
    )
    assert [s["pinned"] for s in derived_fields(_call(), facts)["skills"]] == [
        True,
        False,
        False,
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


# --- procedures (#443) -------------------------------------------------------

_STEP, _REFUSED = "procedure.step", "procedure.refused"


def _step(frm, to, kind="step", procedure="no_internet"):
    return (_STEP, {"procedure": procedure, "from": frm, "to": to, "kind": kind})


def _refused(frm, to, procedure="no_internet"):
    return (_REFUSED, {"procedure": procedure, "from": frm, "to": to})


def _procedures(events, call=None):
    facts = CallFacts(procedure_events=tuple(events))
    return derived_fields(call or _call(), facts)["procedures"]


def test_a_procedure_walked_to_its_end_reads_completed():
    """The spec's worked example: a refusal, then the path to `end`."""
    events = [
        _step("lights", "restart"),
        _refused("restart", "after_restart"),
        _step("restart", "after_restart"),
        _step("after_restart", "resolved", kind="end"),
    ]
    assert _procedures(events) == [
        {
            "name": "no_internet",
            "path": ["lights", "restart", "after_restart", "resolved"],
            "completed": True,
            "ended_at": "resolved",
            "escalated_at": None,
            "refused_moves": 1,
        }
    ]


def test_a_move_back_is_on_the_path_and_the_path_ends_where_the_call_did():
    events = [
        _step("lights", "restart"),
        _step("restart", "lights"),
        _step("lights", "power"),
    ]
    [procedure] = _procedures(events)
    assert procedure["path"] == ["lights", "restart", "lights", "power"]
    assert procedure["ended_at"] == "power"
    assert procedure["completed"] is False


def test_reaching_an_end_step_and_moving_back_off_it_is_not_completed():
    events = [_step("ask", "done", kind="end"), _step("done", "ask")]
    assert _procedures(events)[0]["completed"] is False


def test_a_procedure_ended_on_an_escalate_step_says_where():
    events = [_step("lights", "technician", kind="escalate")]
    [procedure] = _procedures(events)
    assert procedure["escalated_at"] == "technician"
    assert procedure["completed"] is False


def test_a_start_move_from_nowhere_opens_the_path():
    """A move with `from: null` (a procedure started mid-call) has no step
    before it."""
    assert _procedures([_step(None, "lights")])[0]["path"] == ["lights"]


def test_only_refused_moves_still_show_where_the_procedure_stood():
    [procedure] = _procedures([_refused("lights", "resolved")] * 2)
    assert procedure["path"] == ["lights"]
    assert procedure["ended_at"] == "lights"
    assert procedure["refused_moves"] == 2


def test_each_procedure_is_listed_in_the_order_it_first_appears():
    events = [
        _step("a1", "a2", procedure="billing"),
        _refused("x", "y", procedure="no_internet"),
        _step("a2", "a3", procedure="billing"),
    ]
    assert [p["name"] for p in _procedures(events)] == ["billing", "no_internet"]
    assert _procedures(events)[0]["path"] == ["a1", "a2", "a3"]


def test_a_refusal_for_a_procedure_not_running_stands_nowhere():
    """`from` is null when the procedure was not running: no step to stand at."""
    [procedure] = _procedures([_refused(None, "lights", procedure="nope")])
    assert procedure == {
        "name": "nope",
        "path": [],
        "completed": False,
        "ended_at": None,
        "escalated_at": None,
        "refused_moves": 1,
    }


def test_a_call_with_no_procedures_lists_none():
    assert _procedures([]) == []


@pytest.mark.parametrize("expired", ["transcript_expired_at", "events_expired_at"])
def test_the_summary_is_unknown_once_its_events_expired(expired):
    """The events go with the transcript (#316): an empty list would say the
    call followed no procedure."""
    call = SimpleNamespace(status="completed", ended_at=ENDED, **{expired: ENDED})
    assert _procedures([_step("a", "b")], call) is None
