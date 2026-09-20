"""Turn a real call into a scripted scenario draft (#78).

Evals only find what someone thought to test; the unknown unknowns come from
production. A call already holds the shape of a scripted scenario — the
caller's turns, the agent's replies, and the tools it called with which
arguments — so converting one is the loop that makes a scenario library
populate itself instead of rotting at two entries.

**A draft, not a test.** The conversation is captured faithfully; the judgement
about what *should* have happened is the part only a person can supply. The
response says so, because a generated scenario nobody edits is a test that
asserts whatever the agent happened to do that day, including its mistakes.

Pure functions over already-loaded rows: the endpoint does the I/O, so the
rules that matter — turn ordering, which tool call belongs to which turn, what
becomes a mock — are testable without a database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Pipecat's event for the agent's text. `response` would be the transcription
# of its audio, which a text-modality rerun never produces.
_REPLY_EVENT = "llm_response"

# The agent's side, which the assistant tap writes literally. The caller's role
# is *not* reliably "customer": the tap writes `frame.user_id or "customer"`,
# and pipecat fills `user_id` from a `UserAudioRawFrame` on transports that
# supply one (Daily, LiveKit). So the caller is defined as "not the assistant"
# rather than by a literal that a future transport could quietly change —
# otherwise every caller turn would be dropped and a call with plenty of speech
# would convert to "no caller speech".
_ASSISTANT = "assistant"


class ConversionError(ValueError):
    """The call cannot become a scenario, and the reason is the caller's to fix."""


@dataclass(frozen=True)
class Utterance:
    """One thing somebody said, whatever it was stored as.

    A voice call keeps these as `transcript.final` call events with a
    `{role, text}` payload; a text session keeps them as `sms_messages` rows
    with `role`/`content` columns. Same conversation, two schemas — so the
    conversion takes this and each source brings a reader (#87). Without it the
    converter would have to know about call events, and a session could never
    become a scenario.
    """

    role: str
    text: str
    at: datetime | None


def utterances_from_call_events(events: list[Any]) -> list[Utterance]:
    """`transcript.final` call events, as the taps write them."""
    return [
        Utterance(
            role=(e.payload or {}).get("role", ""),
            text=((e.payload or {}).get("text") or "").strip(),
            at=getattr(e, "internal_timestamp", None),
        )
        for e in events
    ]


def utterances_from_session_messages(messages: list[Any]) -> list[Utterance]:
    """`sms_messages` rows — SMS, the Chat API and WhatsApp text.

    `system` rows are skipped: they are not something either party said, and a
    scripted scenario has nowhere to put them.
    """
    return [
        Utterance(
            role=m.role,
            text=(m.content or "").strip(),
            at=getattr(m, "created_at", None),
        )
        for m in messages
        if m.role != "system"
    ]


def _decoded(raw: Any) -> Any:
    """A tool result as the mock should carry it.

    Stored output is JSON when the tool returned JSON and a string otherwise;
    a mock takes either, so pass through what was actually returned rather than
    re-wrapping it.
    """
    if isinstance(raw, dict) and set(raw) == {"result"}:
        # classify_tool_result wraps a non-JSON result; unwrap so the mock
        # reproduces what the model saw, not our envelope.
        return raw["result"]
    return raw


def build_scenario(
    *,
    transcript: list[Utterance],
    invocations: list[Any],
    name: str,
) -> dict[str, Any]:
    """The draft: pipecat's scenario mapping, plus the mocks it needs to be safe.

    Returns `{"definition": ..., "tool_mocks": ...}` — the two halves a stored
    scenario keeps in separate columns.

    Every recorded tool invocation becomes two things. A `function_call`
    expectation carrying the arguments actually used, so a rerun checks the
    agent still decides to call it — and a **mock seeded with the response that
    tool really returned**, so the draft is safe to run under `mock_only`
    without booking anything a second time. A derived scenario that needed a
    human to add mocks before it was safe would mostly be run before they did.
    """
    spoken = [e for e in transcript if e.text and e.role]
    if not any(e.role != _ASSISTANT for e in spoken):
        raise ConversionError("the call has no caller speech to build turns from")

    turns: list[dict[str, Any]] = []
    # When each turn's user utterance was spoken, so a tool call can be
    # attributed to the turn it happened in rather than to the whole call.
    turn_started: list[datetime | None] = []

    for entry in spoken:
        if entry.role != _ASSISTANT:
            if turns and not turns[-1]["expect"]:
                # Consecutive caller entries with no reply between them are one
                # turn of speech that the STT reported in pieces. Seen in real
                # calls: "Hi. Good morning. What" / "do you have in the menu?"
                # became two turns, the first asserting nothing and the second
                # opening mid-sentence. Joining them reproduces what the caller
                # actually said, which is the whole promise of deriving from a
                # call.
                turns[-1]["user"] = f"{turns[-1]['user']} {entry.text}".strip()
                continue
            turns.append({"user": entry.text, "expect": []})
            turn_started.append(entry.at)
        elif turns:
            # The agent's actual words become the assertion to sharpen. Not a
            # bare `{"event": "llm_response"}`: after a provider 404 pipecat
            # still emits an empty response, so an expectation that asserts
            # only the event can pass with the LLM entirely broken
            # (evals-design §1). Content, then, even though the human will trim
            # it to the phrase that matters.
            turns[-1]["expect"].append(
                {"event": _REPLY_EVENT, "text_contains": entry.text}
            )
        # An assistant greeting before the caller says anything belongs to no
        # turn: `first_message` goes out as a TTSSpeakFrame and is invisible to
        # a text-mode eval anyway.

    mocks = _attach_tool_calls(turns, turn_started, invocations)
    return {"definition": {"name": name, "turns": turns}, "tool_mocks": mocks}


def _attach_tool_calls(
    turns: list[dict[str, Any]],
    turn_started: list[datetime | None],
    invocations: list[Any],
) -> dict[str, Any]:
    """Hang each invocation on the turn it happened in, and seed its mock.

    Attribution is by time: an invocation belongs to the last turn that began
    before it. A tool called before the caller said anything (an on-connect
    lookup) has no turn to hang on and is still mocked, because it will fire
    again on a rerun whether or not anything asserts it.

    One expectation per turn, carrying a `calls:` list — pipecat's shape, which
    matches every named call in any order. Writing `name`/`args` at the top
    level instead parses cleanly and asserts *nothing*, since the parser
    ignores keys it does not know: the same trap as an expectation that asserts
    only that an event arrived.
    """
    mocks: dict[str, Any] = {}
    per_turn: dict[int, list[dict[str, Any]]] = {}

    for invocation in invocations:
        name = invocation.tool_name
        # First result wins: a tool called twice with different answers cannot
        # be represented by one mock, and the later one would silently rewrite
        # what the earlier turn was asserting against.
        if name not in mocks:
            mocks[name] = _decoded(invocation.output_json)

        index = _turn_for(turn_started, invocation.created_at)
        if index is None:
            continue
        # The arguments the agent actually chose. A rerun that calls the tool
        # with different ones is a behaviour change worth failing.
        per_turn.setdefault(index, []).append(
            {"name": name, "args": invocation.input_json or {}}
        )

    for index, calls in per_turn.items():
        turns[index]["expect"].append({"event": "function_call", "calls": calls})
    return mocks


def _turn_for(turn_started: list[datetime | None], when: Any) -> int | None:
    if when is None:
        return len(turn_started) - 1 if turn_started else None
    index = None
    for i, started in enumerate(turn_started):
        if started is not None and started <= when:
            index = i
    return index


def default_target(call: Any, config: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the draft should be run against: the agent this call actually used.

    A call whose agent came from call-init as an inline config has **no agent
    row** — ADR-0017 — so the target is that configuration, read off the call
    record. Pointing at `active_agent_id` there would name the zero-UUID
    sentinel or nothing at all.
    """
    if call.active_agent_id:
        return {"type": "agent", "agent_id": str(call.active_agent_id)}
    if config:
        return {"type": "inline", "agent": config}
    return None


def summarise(draft: dict[str, Any]) -> str:
    """One line for the response, so nobody mistakes a draft for a test."""
    turns = draft["definition"]["turns"]
    mocks = draft["tool_mocks"]
    return (
        f"{len(turns)} turn(s) and {len(mocks)} tool mock(s) captured from the "
        "call. The conversation is faithful; the expectations are a draft — "
        "trim each `text_contains` to the part that actually matters before "
        "trusting a green run."
    )


def as_json(draft: dict[str, Any]) -> str:
    """The draft as the file the CLI takes, for a caller that wants to edit it."""
    return json.dumps(draft, indent=2, default=str)
