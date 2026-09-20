"""A real call becomes a scripted scenario draft (#78).

Evals only find what someone thought to test; the unknown unknowns come from
production. What this file protects is the difference between *faithful* and
*finished*: the conversation is captured exactly, the expectations are a draft,
and the tool mocks are seeded so the draft is safe to run before anyone edits
it.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from turncall.services import scenario_from_call as convert

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _entry(role: str, text: str, *, at: int = 0):
    return SimpleNamespace(
        payload={"role": role, "text": text},
        internal_timestamp=T0 + timedelta(seconds=at),
    )


def _invocation(name: str, args: dict, output, *, at: int = 0):
    return SimpleNamespace(
        tool_name=name,
        input_json=args,
        output_json=output,
        created_at=T0 + timedelta(seconds=at),
    )


class TestTurns:
    def test_caller_speech_becomes_turns_and_replies_become_expectations(self) -> None:
        draft = convert.build_scenario(
            transcript=[
                _entry("customer", "what are your hours?", at=1),
                _entry("assistant", "We are open until nine.", at=2),
                _entry("customer", "and on sunday?", at=3),
                _entry("assistant", "Sundays we close at five.", at=4),
            ],
            invocations=[],
            name="hours",
        )
        turns = draft["definition"]["turns"]
        assert [t["user"] for t in turns] == ["what are your hours?", "and on sunday?"]
        assert turns[0]["expect"] == [
            {"event": "llm_response", "text_contains": "We are open until nine."}
        ]
        assert turns[1]["expect"][0]["text_contains"] == "Sundays we close at five."

    def test_the_expectation_asserts_content_not_merely_the_event(self) -> None:
        """After a provider 404 pipecat still emits an empty `llm_response`, so
        a bare `{"event": "llm_response"}` can pass with the LLM entirely
        broken (evals-design §1). A draft that asserts nothing is worse than no
        draft."""
        draft = convert.build_scenario(
            transcript=[
                _entry("customer", "hi", at=1),
                _entry("assistant", "Hello there.", at=2),
            ],
            invocations=[],
            name="greets",
        )
        expectation = draft["definition"]["turns"][0]["expect"][0]
        assert expectation["text_contains"] == "Hello there."

    def test_a_greeting_before_the_caller_speaks_belongs_to_no_turn(self) -> None:
        """`first_message` goes out as a TTSSpeakFrame and is invisible to a
        text-mode eval anyway."""
        draft = convert.build_scenario(
            transcript=[
                _entry("assistant", "Thanks for calling!", at=0),
                _entry("customer", "hi", at=1),
                _entry("assistant", "Hello.", at=2),
            ],
            invocations=[],
            name="greets",
        )
        turns = draft["definition"]["turns"]
        assert len(turns) == 1
        assert [e["text_contains"] for e in turns[0]["expect"]] == ["Hello."]

    def test_a_sentence_the_stt_split_becomes_one_turn(self) -> None:
        """From a real call: "Hi. Good morning. What" / "do you have in the
        menu?" arrived as two transcript entries with no reply between them.
        Kept apart they made a turn that asserts nothing followed by one
        opening mid-sentence — neither of which the caller ever said."""
        draft = convert.build_scenario(
            transcript=[
                _entry("customer", "Hi. Good morning. What", at=1),
                _entry("customer", "do you have in the menu?", at=2),
                _entry("assistant", "We have fresh pasta and seafood.", at=3),
            ],
            invocations=[],
            name="menu",
        )
        turns = draft["definition"]["turns"]
        assert [t["user"] for t in turns] == [
            "Hi. Good morning. What do you have in the menu?"
        ]
        assert turns[0]["expect"][0]["text_contains"] == "We have fresh pasta and seafood."

    def test_a_second_utterance_after_a_reply_is_its_own_turn(self) -> None:
        """Merging is only for caller speech with nothing in between: once the
        agent has answered, the next thing the caller says is a new turn."""
        draft = convert.build_scenario(
            transcript=[
                _entry("customer", "Hi.", at=1),
                _entry("assistant", "Good morning!", at=2),
                _entry("customer", "What are your hours?", at=3),
                _entry("assistant", "Until nine.", at=4),
            ],
            invocations=[],
            name="hours",
        )
        assert [t["user"] for t in draft["definition"]["turns"]] == [
            "Hi.",
            "What are your hours?",
        ]

    def test_several_replies_to_one_turn_are_all_kept(self) -> None:
        draft = convert.build_scenario(
            transcript=[
                _entry("customer", "tell me everything", at=1),
                _entry("assistant", "First this.", at=2),
                _entry("assistant", "Then that.", at=3),
            ],
            invocations=[],
            name="verbose",
        )
        assert len(draft["definition"]["turns"][0]["expect"]) == 2

    def test_the_caller_is_whoever_is_not_the_assistant(self) -> None:
        """The tap writes `frame.user_id or "customer"`, and pipecat fills
        `user_id` from a `UserAudioRawFrame` on transports that supply one
        (Daily, LiveKit). Keying on the literal "customer" would drop every
        caller turn on such a transport and report a call full of speech as
        having none."""
        draft = convert.build_scenario(
            transcript=[
                _entry("participant-7f3a", "do you deliver?", at=1),
                _entry("assistant", "We do, within five miles.", at=2),
            ],
            invocations=[],
            name="delivery",
        )
        turns = draft["definition"]["turns"]
        assert [t["user"] for t in turns] == ["do you deliver?"]
        assert turns[0]["expect"][0]["text_contains"] == "We do, within five miles."

    def test_a_call_with_no_caller_speech_is_refused(self) -> None:
        """A voicemail or a call that never connected has nothing to script."""
        with pytest.raises(convert.ConversionError, match="no caller speech"):
            convert.build_scenario(
                transcript=[_entry("assistant", "Thanks for calling!", at=0)],
                invocations=[],
                name="empty",
            )

    def test_blank_entries_are_skipped(self) -> None:
        draft = convert.build_scenario(
            transcript=[
                _entry("customer", "   ", at=1),
                _entry("customer", "hello?", at=2),
                _entry("assistant", "", at=3),
            ],
            invocations=[],
            name="sparse",
        )
        assert [t["user"] for t in draft["definition"]["turns"]] == ["hello?"]


class TestToolCalls:
    def test_an_invocation_becomes_an_expectation_with_its_real_arguments(
        self,
    ) -> None:
        """A rerun that calls the tool with different arguments is a behaviour
        change worth failing."""
        draft = convert.build_scenario(
            transcript=[
                _entry("customer", "book me for friday", at=1),
                _entry("assistant", "Booked for Friday at two.", at=4),
            ],
            invocations=[
                _invocation(
                    "book_appointment",
                    {"day": "friday", "time": "14:00"},
                    {"status": "confirmed", "id": "APT-9"},
                    at=2,
                )
            ],
            name="books",
        )
        expect = draft["definition"]["turns"][0]["expect"]
        expectation = next(e for e in expect if e["event"] == "function_call")
        # Pipecat's shape is a `calls:` list. Top-level name/args parses fine
        # and asserts nothing, because the parser ignores keys it does not
        # know — the same trap as asserting only that an event arrived.
        assert expectation["calls"] == [
            {"name": "book_appointment", "args": {"day": "friday", "time": "14:00"}}
        ]

    def test_the_mock_is_seeded_with_what_the_tool_really_returned(self) -> None:
        """So the draft is safe to run under `mock_only` without booking the
        appointment a second time. A derived scenario that needed a human to
        add mocks before it was safe would mostly be run before they did."""
        draft = convert.build_scenario(
            transcript=[_entry("customer", "book me", at=1)],
            invocations=[
                _invocation("book_appointment", {}, {"status": "confirmed"}, at=2)
            ],
            name="books",
        )
        assert draft["tool_mocks"] == {"book_appointment": {"status": "confirmed"}}

    def test_a_wrapped_plain_result_is_unwrapped(self) -> None:
        """`classify_tool_result` wraps a non-JSON result; the mock should
        reproduce what the model saw, not our envelope."""
        draft = convert.build_scenario(
            transcript=[_entry("customer", "check stock", at=1)],
            invocations=[_invocation("check_stock", {}, {"result": "in stock"}, at=2)],
            name="stock",
        )
        assert draft["tool_mocks"] == {"check_stock": "in stock"}

    def test_a_call_lands_on_the_turn_it_happened_in(self) -> None:
        draft = convert.build_scenario(
            transcript=[
                _entry("customer", "what are your hours?", at=1),
                _entry("assistant", "Until nine.", at=2),
                _entry("customer", "book me then", at=3),
                _entry("assistant", "Done.", at=6),
            ],
            invocations=[_invocation("book_appointment", {}, {"ok": True}, at=4)],
            name="books",
        )
        turns = draft["definition"]["turns"]
        assert not [e for e in turns[0]["expect"] if e["event"] == "function_call"]
        assert [e for e in turns[1]["expect"] if e["event"] == "function_call"]

    def test_the_same_tool_twice_keeps_the_first_answer(self) -> None:
        """One mock cannot represent two different answers, and the later one
        would silently rewrite what the earlier turn asserts against."""
        draft = convert.build_scenario(
            transcript=[_entry("customer", "check twice", at=1)],
            invocations=[
                _invocation("lookup", {}, {"n": 1}, at=2),
                _invocation("lookup", {}, {"n": 2}, at=3),
            ],
            name="lookups",
        )
        assert draft["tool_mocks"] == {"lookup": {"n": 1}}
        # Both calls, in one expectation: pipecat matches the list by name in
        # any order.
        expectation = draft["definition"]["turns"][0]["expect"][0]
        assert [c["name"] for c in expectation["calls"]] == ["lookup", "lookup"]

    def test_a_tool_called_before_any_turn_is_still_mocked(self) -> None:
        """It will fire again on a rerun whether or not anything asserts it."""
        draft = convert.build_scenario(
            transcript=[_entry("customer", "hello", at=5)],
            invocations=[_invocation("lookup_caller", {}, {"name": "Jo"}, at=1)],
            name="greets",
        )
        assert draft["tool_mocks"] == {"lookup_caller": {"name": "Jo"}}
        assert draft["definition"]["turns"][0]["expect"] == []


class TestTheDraftIsRunnable:
    def test_it_parses_the_way_a_hand_written_scenario_does(self) -> None:
        """A draft that cannot be parsed is not a draft, it is a bug report."""
        from turncall.domain.enums import EvalModality
        from turncall.evals.scenario import parse, with_modality

        draft = convert.build_scenario(
            transcript=[
                _entry("customer", "book me for friday", at=1),
                _entry("assistant", "Booked.", at=3),
            ],
            invocations=[_invocation("book_appointment", {"day": "fri"}, {}, at=2)],
            name="books",
        )
        parsed = parse(
            with_modality(draft["definition"], EvalModality.TEXT), name="books"
        )
        assert len(parsed.turns) == 1
        # Parsing is not proof: assert pipecat actually *kept* the calls, since
        # an expectation it silently ignored would assert nothing on a rerun.
        expectation = next(
            e for e in parsed.turns[0].expect if e.event == "function_call"
        )
        assert [c.name for c in expectation.calls] == ["book_appointment"]
        assert expectation.calls[0].args == {"day": "fri"}


class TestTarget:
    def test_a_stored_agent_becomes_a_pinned_target(self) -> None:
        agent_id = uuid4()
        call = SimpleNamespace(active_agent_id=agent_id)
        assert convert.default_target(call, {"system_prompt": "x"}) == {
            "type": "agent",
            "agent_id": str(agent_id),
        }

    def test_an_inline_agent_is_read_off_the_call_record(self) -> None:
        """ADR-0017: a call whose agent came from call-init has no agent row,
        so pointing at `active_agent_id` would name the zero-UUID sentinel or
        nothing at all."""
        call = SimpleNamespace(active_agent_id=None)
        config = {"system_prompt": "from call-init"}
        assert convert.default_target(call, config) == {
            "type": "inline",
            "agent": config,
        }

    def test_no_agent_at_all_leaves_the_target_unset(self) -> None:
        call = SimpleNamespace(active_agent_id=None)
        assert convert.default_target(call, None) is None


def test_the_note_says_it_is_a_draft() -> None:
    """A generated scenario nobody edits asserts whatever the agent happened to
    do that day, mistakes included."""
    draft = convert.build_scenario(
        transcript=[
            _entry("customer", "hi", at=1),
            _entry("assistant", "Hello.", at=2),
        ],
        invocations=[],
        name="greets",
    )
    note = convert.summarise(draft)
    assert "draft" in note and "trim" in note
