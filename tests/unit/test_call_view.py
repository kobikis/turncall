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
