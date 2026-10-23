"""Redaction's rules, on a stand-in detector (#313, ADR-0024).

Which strings of a payload are text, what a phone field becomes, that the
caller's dict is left alone, that a payload with no text never reaches the
detector, that a broken detector withholds rather than leaks and is not
retried on every event, that detection runs off the event loop, the policy
cache, the exit point's rule that nothing waits on the detector inside a
transaction, and that nothing imports Presidio until a project with redaction
on needs it. The real detector, in its worker process, is
`test_redaction_detector.py`; the whole path is the harness's
`test_harness_redaction.py`.

The stand-in runs on a thread pool in place of the worker process, which a
monkeypatched detector could not reach.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.projects import ProjectRedaction
from turncall.events import emit
from turncall.services import redaction
from turncall.services.redaction import DEFAULT_ENTITIES, RedactionPolicy, policy_of

pytestmark = pytest.mark.unit

SECRET = "jane.doe@example.com"


class _Marks:
    """Replaces SECRET wherever it appears, and records where it ran."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.seen: list[str] = []
        self.threads: set[int] = set()
        self._delay = delay

    def redact(self, text: str, entities: tuple[str, ...]) -> str:
        self.threads.add(threading.get_ident())
        self.seen.append(text)
        if self._delay:
            time.sleep(self._delay)
        return text.replace(SECRET, "<EMAIL_ADDRESS>")


def _use(monkeypatch, build) -> None:
    """A fresh engine whose worker is a thread building `build()`."""
    monkeypatch.setattr(redaction, "_DETECTOR", None)
    monkeypatch.setattr(redaction, "_build_detector", build)
    monkeypatch.setattr(
        redaction, "_new_pool", lambda: ThreadPoolExecutor(max_workers=1)
    )
    monkeypatch.setattr(redaction, "ENGINE", redaction._Engine())


@pytest.fixture
def detector(monkeypatch) -> Iterator[_Marks]:
    marks = _Marks()
    _use(monkeypatch, lambda: marks)
    yield marks
    redaction.ENGINE.shutdown()


POLICY = RedactionPolicy(DEFAULT_ENTITIES)


async def test_text_anywhere_in_a_payload_is_redacted(detector) -> None:
    payload = {
        "text": f"Write to {SECRET}",
        "arguments": {"contact": {"email": SECRET}, "tags": [SECRET, 3]},
        "transcript": [{"role": "customer", "text": f"it's {SECRET}"}],
    }

    out = await redaction.redact_payload(payload, POLICY)

    assert out == {
        "text": "Write to <EMAIL_ADDRESS>",
        "arguments": {
            "contact": {"email": "<EMAIL_ADDRESS>"},
            "tags": ["<EMAIL_ADDRESS>", 3],
        },
        "transcript": [{"role": "customer", "text": "it's <EMAIL_ADDRESS>"}],
    }


async def test_the_callers_payload_is_left_as_it_was(detector) -> None:
    payload = {"text": SECRET, "nested": {"text": SECRET}}

    await redaction.redact_payload(payload, POLICY)

    assert payload == {"text": SECRET, "nested": {"text": SECRET}}


async def test_turncalls_own_fields_are_never_analysed(detector) -> None:
    """Ids, timestamps, URLs and enum fields at the top level, and anything
    shaped like a UUID or a timestamp at any depth: no caller text there, and a
    detector could read a digit run in one as a number."""
    payload = {
        "call_id": SECRET,
        "provider_call_sid": SECRET,
        "started_at": SECRET,
        "recording_url": SECRET,
        "status": SECRET,
        "role": SECRET,
        "metadata": {
            "ref": "550e8400-e29b-41d4-a716-446655440000",
            "when": "2026-10-16T12:00:00+00:00",
        },
    }

    out = await redaction.redact_payload(payload, POLICY)

    assert out == payload
    assert detector.seen == []


async def test_a_phone_field_is_replaced_whole_when_phone_numbers_are_chosen(
    detector,
) -> None:
    """Whatever the detector makes of its format: it reads `+15550001234` (a
    555 number) as no phone number at all."""
    payload = {
        "from_number": "+15550001234",
        "to_number": "15550009999",
        "customer_number": "+44 20 7946 0958",
        "target_number": "+15559990000",
    }

    out = await redaction.redact_payload(payload, POLICY)

    assert set(out.values()) == {"<PHONE_NUMBER>"}


async def test_a_phone_field_without_a_number_is_text(detector) -> None:
    """A WebRTC call's "number" is no number; a SIP address may hold an email."""
    out = await redaction.redact_payload(
        {"from_number": "webrtc", "to_number": f"sip:{SECRET}"}, POLICY
    )

    assert out == {"from_number": "webrtc", "to_number": "sip:<EMAIL_ADDRESS>"}


async def test_phone_fields_stay_when_phone_numbers_are_not_chosen(detector) -> None:
    out = await redaction.redact_payload(
        {"from_number": "+15550001234"}, RedactionPolicy(("EMAIL_ADDRESS",))
    )

    assert out == {"from_number": "+15550001234"}


async def test_a_payload_with_no_text_never_reaches_the_detector(
    monkeypatch,
) -> None:
    """Ids, numbers and structure need no detector: neither it nor its worker
    is started for them (finding 2 of #327's review)."""
    built: list[str] = []
    _use(monkeypatch, lambda: built.append("built") or _Marks())

    out = await redaction.redact_payload(
        {
            "from_number": "+1 (555) 000-1234",
            "to_number": "whatsapp:+15550009999",
            "provider_call_sid": "CA123",
            "direction": "inbound",
            "dynamic_config": True,
            "duration_ms": 1200,
        },
        POLICY,
        event_type="call.started",
    )

    assert out["from_number"] == out["to_number"] == "<PHONE_NUMBER>"
    assert built == []
    assert redaction.ENGINE._pool is None


async def test_why_an_avatar_did_not_show_never_reaches_the_detector(
    monkeypatch,
) -> None:
    """`avatar.unavailable` is TurnCall's own vocabulary, provider and reason
    alike (#402): nothing in it for a detector to read."""
    built: list[str] = []
    _use(monkeypatch, lambda: built.append("built") or _Marks())
    payload = {"provider": "heygen", "reason": "session_refused"}

    out = await redaction.redact_payload(
        payload, POLICY, event_type="avatar.unavailable"
    )

    assert out == payload
    assert built == []


async def test_identifiers_read_back_as_data_are_not_analysed(detector) -> None:
    """A provider or tool name is no ORGANIZATION: `provider.failover`'s
    `from`/`to` and `tool_name` are TurnCall's own vocabulary, while a reason
    the model gave in its own words is still text."""
    failover = {"role": "llm", "from": SECRET, "to": SECRET, "error": SECRET}
    tool = {"tool_name": SECRET, "arguments": {"q": SECRET}}
    transferred = {"reason": SECRET, "transfer_mode": "warm"}

    out = [
        await redaction.redact_payload(
            failover, POLICY, event_type="provider.failover"
        ),
        await redaction.redact_payload(tool, POLICY, event_type="tool.result"),
        await redaction.redact_payload(
            transferred, POLICY, event_type="call.transferred"
        ),
    ]

    assert out[0] == {**failover, "error": "<EMAIL_ADDRESS>"}
    assert out[1] == {"tool_name": SECRET, "arguments": {"q": "<EMAIL_ADDRESS>"}}
    assert out[2] == {"reason": "<EMAIL_ADDRESS>", "transfer_mode": "warm"}
    # The same `from` on another event is text.
    other = await redaction.redact_payload({"from": SECRET}, POLICY, event_type="x")
    assert other == {"from": "<EMAIL_ADDRESS>"}


async def test_what_was_stored_redacted_passes_through(detector) -> None:
    """`call.ended`'s transcript is read back from rows redacted when they
    were stored: it is not analysed again (finding 7)."""
    out = await redaction.redact_payload(
        {"transcript": [{"text": SECRET}], "metadata": {"note": SECRET}},
        POLICY,
        keep=("transcript",),
    )

    assert out == {
        "transcript": [{"text": SECRET}],
        "metadata": {"note": "<EMAIL_ADDRESS>"},
    }
    assert detector.seen[-1:] == [SECRET]  # once, for the metadata
    assert detector.seen.count(SECRET) == 1


# --- Free text and free JSON (#314) -------------------------------------------


async def test_a_plain_string_is_redacted_as_it_is(detector) -> None:
    """An SMS row's content, a log line: no `{"text": s}` wrapping."""
    assert await redaction.redact_text(f"mail {SECRET}", POLICY) == (
        "mail <EMAIL_ADDRESS>"
    )
    assert await redaction.redact_text("  ", POLICY) == "  "
    assert detector.seen[-1] == f"mail {SECRET}"
    assert "  " not in detector.seen


async def test_free_json_keys_are_the_users_and_are_all_analysed(detector) -> None:
    """A tool's arguments or a takeaway: a field named `customer_id`,
    `callback_url`, `status` or `number` is the user's, and may hold what the
    caller said. Nothing is skipped by its key, and a phone-named field is
    analysed by the detector rather than replaced whole."""
    value = {
        "customer_id": SECRET,
        "callback_url": f"mailto:{SECRET}",
        "status": SECRET,
        "role": SECRET,
        "number": f"call {SECRET}",
        "from_number": "+15550001234",
        "nested": {"order_id": SECRET},
    }

    out = await redaction.redact_json(value, POLICY)

    assert out == {
        "customer_id": "<EMAIL_ADDRESS>",
        "callback_url": "mailto:<EMAIL_ADDRESS>",
        "status": "<EMAIL_ADDRESS>",
        "role": "<EMAIL_ADDRESS>",
        "number": "call <EMAIL_ADDRESS>",
        # The stand-in finds no number in it; the real detector's phone
        # recognizer is what reads it (`test_redaction_detector.py`).
        "from_number": "+15550001234",
        "nested": {"order_id": "<EMAIL_ADDRESS>"},
    }
    assert "+15550001234" in detector.seen


async def test_free_json_may_be_any_json_value(detector) -> None:
    """A tool's result is whatever its webhook answered: a list, a string, a
    number. The caller's value is left as it was."""
    listed = [SECRET, {"a": [SECRET, 1, None, True]}]

    assert await redaction.redact_json(listed, POLICY) == [
        "<EMAIL_ADDRESS>",
        {"a": ["<EMAIL_ADDRESS>", 1, None, True]},
    ]
    assert listed == [SECRET, {"a": [SECRET, 1, None, True]}]
    assert await redaction.redact_json(SECRET, POLICY) == "<EMAIL_ADDRESS>"
    assert await redaction.redact_json(42, POLICY) == 42
    assert await redaction.redact_json(None, POLICY) is None


async def test_free_json_analyses_long_integers_as_their_digits(monkeypatch) -> None:
    """A tool's webhook may answer a card or a phone number as a JSON number.
    It is analysed as the digits it is written with; one the detector finds
    becomes its tag, any other stays a number. Counts never reach it."""

    class Cards(_Marks):
        def redact(self, text: str, entities: tuple[str, ...]) -> str:
            self.seen.append(text)
            return {"4111111111111111": "<CREDIT_CARD>"}.get(text, text)

    cards = Cards()
    _use(monkeypatch, lambda: cards)
    value = {
        "card_number": 4111111111111111,
        "order": 98765432,
        "items": [4111111111111111, 3],
        "total": 4111111111111111.0,
        "paid": True,
        "party_size": 4,
    }
    try:
        out = await redaction.redact_json(value, POLICY)
    finally:
        redaction.ENGINE.shutdown()

    assert out == {
        "card_number": "<CREDIT_CARD>",
        "order": 98765432,
        "items": ["<CREDIT_CARD>", 3],
        "total": 4111111111111111.0,
        "paid": True,
        "party_size": 4,
    }
    assert value["card_number"] == 4111111111111111
    assert {"4111111111111111", "98765432"} <= set(cards.seen)
    assert not {"3", "4", "True"} & set(cards.seen)


async def test_free_json_long_integers_are_withheld_when_the_detector_fails(
    monkeypatch,
) -> None:
    def broken() -> None:
        raise RuntimeError("model missing")

    _use(monkeypatch, broken)
    try:
        out = await redaction.redact_json({"phone": 4155550132, "n": 12}, POLICY)
    finally:
        redaction.ENGINE.shutdown()

    assert out == {"phone": "<REDACTED>", "n": 12}


async def test_a_payloads_nested_long_integers_are_analysed_as_free_json_s_are(
    monkeypatch,
) -> None:
    """A tool's arguments or the call's metadata may carry a phone number as a
    JSON number (`{"phone": 4155550132}`): below the top level, a payload's
    long integers go to the detector as `redact_json`'s do. A number at the
    top level is TurnCall's own measure (`duration_ms`) and is never analysed."""

    class Phones(_Marks):
        def redact(self, text: str, entities: tuple[str, ...]) -> str:
            self.seen.append(text)
            return {"4155550132": "<PHONE_NUMBER>"}.get(text, text)

    phones = Phones()
    _use(monkeypatch, lambda: phones)
    payload = {
        "tool_name": "book",
        "arguments": {"phone": 4155550132, "order": 98765432, "party": 4},
        "metadata": {"callback": [4155550132]},
        "duration_ms": 4155550132,
    }
    try:
        out = await redaction.redact_payload(payload, POLICY, event_type="tool.result")
    finally:
        redaction.ENGINE.shutdown()

    assert out == {
        "tool_name": "book",
        "arguments": {"phone": "<PHONE_NUMBER>", "order": 98765432, "party": 4},
        "metadata": {"callback": ["<PHONE_NUMBER>"]},
        "duration_ms": 4155550132,
    }
    assert {"4155550132", "98765432"} <= set(phones.seen)
    assert "4" not in phones.seen
    assert redaction.texts_in({"duration_ms": 4155550132, "n": {"k": 12}}) == []


async def test_a_payloads_nested_long_integers_are_withheld_without_the_detector(
    monkeypatch,
) -> None:
    def broken() -> None:
        raise RuntimeError("model missing")

    _use(monkeypatch, broken)
    try:
        out = await redaction.redact_payload(
            {"arguments": {"phone": 4155550132, "n": 12}, "duration_ms": 1200000},
            POLICY,
        )
    finally:
        redaction.ENGINE.shutdown()

    assert out == {
        "arguments": {"phone": "<REDACTED>", "n": 12},
        "duration_ms": 1200000,
    }


async def test_free_json_skips_uuids_and_timestamps_by_their_shape(detector) -> None:
    value = {
        "ref": "550e8400-e29b-41d4-a716-446655440000",
        "when": ["2026-10-16T12:00:00+00:00"],
    }

    assert await redaction.redact_json(value, POLICY) == value
    assert detector.seen == []


async def test_free_json_with_no_text_never_reaches_the_detector(monkeypatch) -> None:
    built: list[str] = []
    _use(monkeypatch, lambda: built.append("built") or _Marks())

    out = await redaction.redact_json({"n": 3, "ok": True, "items": []}, POLICY)

    assert out == {"n": 3, "ok": True, "items": []}
    assert built == []
    assert redaction.ENGINE._pool is None


async def test_free_text_and_json_withhold_when_the_detector_fails(
    monkeypatch,
) -> None:
    def broken() -> None:
        raise RuntimeError("model missing")

    _use(monkeypatch, broken)
    try:
        text = await redaction.redact_text(SECRET, POLICY)
        value = await redaction.redact_json(
            {"customer_id": SECRET, "n": 1, "from_number": "+15550001234"}, POLICY
        )
    finally:
        redaction.ENGINE.shutdown()

    assert text == "<REDACTED>"
    # No phone-field rule in free JSON: the number is text, withheld with it.
    assert value == {"customer_id": "<REDACTED>", "n": 1, "from_number": "<REDACTED>"}


async def test_strict_raises_rather_than_withholds(monkeypatch) -> None:
    """For a rewrite of stored rows, which would rather try again later than
    store `<REDACTED>` for good."""

    def broken() -> None:
        raise RuntimeError("model missing")

    _use(monkeypatch, broken)
    try:
        with pytest.raises(redaction.RedactionWithheld):
            await redaction.redact_json([SECRET], POLICY, strict=True)
        with pytest.raises(redaction.RedactionWithheld):
            await redaction.redact_payload({"text": SECRET}, POLICY, strict=True)
        # No text, no detector: nothing to withhold.
        assert await redaction.redact_payload(
            {"customer_number": "+15550001234"}, POLICY, strict=True
        ) == {"customer_number": "<PHONE_NUMBER>"}
    finally:
        redaction.ENGINE.shutdown()


async def test_free_json_past_the_backlog_is_withheld(monkeypatch) -> None:
    slow = _Marks(delay=0.2)
    _use(monkeypatch, lambda: slow)
    monkeypatch.setattr(redaction, "MAX_PENDING", 1)
    try:
        assert await redaction.ready()
        first = asyncio.create_task(redaction.redact_text(SECRET, POLICY))
        await asyncio.sleep(0.05)
        over = await redaction.redact_json({"a": SECRET}, POLICY)
        done = await first
    finally:
        redaction.ENGINE.shutdown()

    assert over == {"a": "<REDACTED>"}
    assert done == "<EMAIL_ADDRESS>"


async def test_a_failing_detector_withholds_the_text(monkeypatch) -> None:
    """Never raw because redaction broke: every text string is withheld, and
    the event still goes out."""

    def broken() -> None:
        raise RuntimeError("model missing")

    _use(monkeypatch, broken)

    out = await redaction.redact_payload(
        {"text": "hello", "call_id": "kept", "from_number": "+15550001234"}, POLICY
    )

    assert out == {
        "text": "<REDACTED>",
        "call_id": "kept",
        "from_number": "<PHONE_NUMBER>",  # no detector needed for that
    }


async def test_a_failed_load_is_not_retried_until_the_back_off_is_over(
    monkeypatch,
) -> None:
    """A detector that cannot load is not loaded again for every event: the
    breaker withholds at once, logs once per window, and tries again after
    the back-off (finding 5)."""
    from loguru import logger

    attempts: list[int] = []
    healthy = _Marks()

    def build() -> _Marks:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("model missing")
        return healthy

    _use(monkeypatch, build)
    logged: list[str] = []
    sink = logger.add(lambda m: logged.append(m.record["message"]), level="WARNING")
    try:
        for _ in range(5):
            out = await redaction.redact_payload({"text": SECRET}, POLICY)
            assert out == {"text": "<REDACTED>"}
        assert len(attempts) == 1
        assert logged.count("redaction_unavailable") == 1
        assert logged.count("redaction_withheld_detector_unavailable") == 1

        # The back-off is over: it loads again, and redacts.
        redaction.ENGINE._closed_until = 0.0
        out = await redaction.redact_payload({"text": SECRET}, POLICY)
    finally:
        logger.remove(sink)
        redaction.ENGINE.shutdown()

    assert out == {"text": "<EMAIL_ADDRESS>"}
    assert len(attempts) == 2


async def test_a_backlog_past_the_bound_is_withheld_not_queued(monkeypatch) -> None:
    """A redaction worker minutes behind would hold every event that long:
    past `MAX_PENDING`, text is withheld at once, and that is logged."""
    slow = _Marks(delay=0.2)
    _use(monkeypatch, lambda: slow)
    monkeypatch.setattr(redaction, "MAX_PENDING", 2)
    try:
        assert await redaction.ready()
        first = asyncio.create_task(
            redaction.redact_payload({"a": f"1 {SECRET}", "b": f"2 {SECRET}"}, POLICY)
        )
        await asyncio.sleep(0.05)
        over = await redaction.redact_payload({"c": SECRET}, POLICY)
        done = await first
    finally:
        redaction.ENGINE.shutdown()

    assert over == {"c": "<REDACTED>"}
    assert done == {"a": "1 <EMAIL_ADDRESS>", "b": "2 <EMAIL_ADDRESS>"}


async def test_a_slow_redaction_is_logged_with_its_backlog(monkeypatch) -> None:
    from loguru import logger

    _use(monkeypatch, lambda: _Marks(delay=0.15))
    monkeypatch.setattr(redaction, "SLOW_SECONDS", 0.1)
    logged: list[dict] = []
    sink = logger.add(
        lambda m: (
            logged.append(m.record) if m.record["message"] == "redaction_slow" else None
        )
    )
    try:
        assert await redaction.ready()
        await redaction.redact_payload({"text": SECRET}, POLICY)
    finally:
        logger.remove(sink)
        redaction.ENGINE.shutdown()

    assert [r["extra"]["texts"] for r in logged] == [1]
    assert "backlog" in logged[0]["extra"]


async def test_detection_runs_off_the_event_loop(monkeypatch) -> None:
    """A slow detection (a whole transcript takes hundreds of ms) leaves the
    loop free to keep ticking: on the loop it would be dead air on every live
    call in the process (ADR-0004)."""
    slow = _Marks(delay=0.3)
    _use(monkeypatch, lambda: slow)
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        await redaction.redact_payload({"text": SECRET}, POLICY)
    finally:
        ticker.cancel()
        redaction.ENGINE.shutdown()

    assert threading.get_ident() not in slow.threads
    assert ticks >= 10, f"the loop was blocked: {ticks} ticks in 300ms"


async def test_the_detector_is_built_once(monkeypatch) -> None:
    built: list[_Marks] = []

    def build() -> _Marks:
        built.append(_Marks())
        return built[-1]

    _use(monkeypatch, build)
    try:
        await asyncio.gather(
            *(redaction.redact_payload({"text": SECRET}, POLICY) for _ in range(5))
        )
    finally:
        redaction.ENGINE.shutdown()

    assert len(built) == 1


async def test_warming_loads_it_in_the_background(monkeypatch) -> None:
    """At startup or when a project turns redaction on: loaded before any call
    needs it, without anyone waiting (finding 2)."""
    built: list[str] = []
    _use(monkeypatch, lambda: built.append("built") or _Marks(delay=0))
    try:
        redaction.warm()
        assert await redaction.ready()
        await redaction.redact_payload({"text": SECRET}, POLICY)
    finally:
        redaction.ENGINE.shutdown()

    assert built == ["built"]


# --- The policy, cached ------------------------------------------------------


async def test_the_policy_is_read_once_per_ttl_and_replaced_by_a_put(
    monkeypatch,
) -> None:
    """One read per project per `POLICY_TTL_SECONDS`, not one per event
    (finding 8); a `PUT` on this process applies at once."""
    reads: list[object] = []
    project_id = uuid4()

    async def read(pid, _sessions=None):
        reads.append(pid)
        return RedactionPolicy(("EMAIL_ADDRESS",))

    monkeypatch.setattr(redaction, "_read_policy_alone", read)
    for _ in range(3):
        assert await redaction.policy_for_project(project_id) == RedactionPolicy(
            ("EMAIL_ADDRESS",)
        )
    assert reads == [project_id]

    redaction.remember_policy(project_id, None)  # PUT {"enabled": false}
    assert await redaction.policy_for_project(project_id) is None
    assert reads == [project_id]

    monkeypatch.setattr(redaction, "POLICY_TTL_SECONDS", 0.0)
    redaction.remember_policy(project_id, None)
    assert await redaction.policy_for_project(project_id) is not None
    assert reads == [project_id, project_id]


def test_off_or_on_with_defaults() -> None:
    assert policy_of(False, ["PERSON"]) is None
    assert policy_of(True, None) == RedactionPolicy(DEFAULT_ENTITIES)
    assert policy_of(True, ["PERSON"]) == RedactionPolicy(("PERSON",))
    assert "PERSON" not in DEFAULT_ENTITIES
    # A type an earlier build stored and this one dropped is ignored.
    assert policy_of(True, ["GONE", "PERSON"]) == RedactionPolicy(("PERSON",))
    assert policy_of(True, ["GONE"]) == RedactionPolicy(DEFAULT_ENTITIES)


def test_the_setting_names_known_entity_types_only() -> None:
    assert ProjectRedaction(enabled=True).entities is None
    assert ProjectRedaction(enabled=True, entities=["PERSON", "PERSON"]).entities == [
        "PERSON"
    ]
    with pytest.raises(ValidationError, match="unknown entity types"):
        ProjectRedaction(enabled=True, entities=["PHONE", "PERSON"])
    with pytest.raises(ValidationError, match="at least one"):
        ProjectRedaction(enabled=True, entities=[])
    with pytest.raises(ValidationError):
        ProjectRedaction(enabled=True, extra=1)  # type: ignore[call-arg]


def test_enabled_is_required() -> None:
    """A PUT naming only `entities` used to store `enabled=false` silently
    (finding 10)."""
    with pytest.raises(ValidationError, match="enabled"):
        ProjectRedaction(entities=["PERSON"])  # type: ignore[call-arg]


def test_a_stored_entity_type_no_longer_supported_is_read_not_refused() -> None:
    """Dropping a type from a later build must not turn every read of a
    project that chose it into a 500 (finding 10)."""
    row = SimpleNamespace(
        id=uuid4(), redaction_enabled=True, redaction_entities=["GONE", "PERSON"]
    )
    gone = SimpleNamespace(
        id=uuid4(), redaction_enabled=True, redaction_entities=["GONE"]
    )

    assert ProjectRedaction.from_row(row) == ProjectRedaction(
        enabled=True, entities=["PERSON"]
    )
    assert ProjectRedaction.from_row(gone).entities is None


# --- The exit point: no detector wait inside a transaction --------------------


class _Session:
    """Enough of a session for the exit point's policy read."""

    def __init__(self, enabled: bool = True) -> None:
        self.project = SimpleNamespace(
            redaction_enabled=enabled, redaction_entities=None
        )

    async def get(self, _model, _id):
        return self.project


async def test_a_plain_payload_with_text_is_refused_under_strict(detector) -> None:
    """Text must come through `emit.redact()`, awaited before the caller's
    transaction. Strict (the test suite) refuses it whatever the policy, so a
    test of the call site fails (finding 1)."""
    for enabled in (True, False):
        with pytest.raises(emit.TextNotRedacted):
            await emit._redact(
                _Session(enabled), uuid4(), "context.injected", {"message": "hi"}
            )
    assert detector.seen == []


async def test_a_plain_payload_of_ids_and_numbers_is_redacted_without_waiting(
    detector,
) -> None:
    out = await emit._redact(
        _Session(),
        uuid4(),
        "call.initializing",
        {"from_number": "+15550001234", "direction": "inbound"},
    )

    assert out == {"from_number": "<PHONE_NUMBER>", "direction": "inbound"}
    assert detector.seen == []


async def test_outside_strict_text_in_a_plain_payload_is_withheld_not_waited_on(
    detector, monkeypatch
) -> None:
    monkeypatch.setattr(emit, "STRICT", False)

    out = await emit._redact(
        _Session(), uuid4(), "context.injected", {"message": SECRET, "role": "system"}
    )

    assert out == {"message": "<REDACTED>", "role": "system"}
    assert detector.seen == []


async def test_redact_waits_with_the_read_ended_and_its_result_passes_through(
    detector, monkeypatch
) -> None:
    project_id = uuid4()

    class Reading(_Session):
        """A session in a read-only transaction: the policy is read on it,
        then the transaction is ended before the wait."""

        committed = False

        def in_transaction(self) -> bool:
            return not self.committed

        async def commit(self) -> None:
            self.committed = True

    reading = Reading()
    prepared = await emit.redact(
        project_id, "context.injected", {"message": SECRET}, release=reading
    )
    out = await emit._redact(
        _Session(), project_id, "context.injected", prepared.plus(success=True)
    )

    assert reading.committed
    assert out == {"message": "<EMAIL_ADDRESS>", "success": True}
    with pytest.raises(ValueError, match=r"redacted for context\.injected"):
        await emit._redact(_Session(), project_id, "dtmf.sent", prepared)
    with pytest.raises(emit.TextNotRedacted):
        prepared.plus(note="text")  # type: ignore[arg-type]


def test_the_app_and_the_call_path_import_no_detector() -> None:
    """Presidio and spaCy cost seconds and ~200MB: no process loads them for a
    project with redaction off, so nothing on the app's import graph may."""
    probe = textwrap.dedent(
        """
        import sys
        import turncall.app
        import turncall.events.emit
        import turncall.orchestrator.pipeline_builder
        import turncall.services.redaction
        import turncall.api.v1.projects
        loaded = sorted(
            m for m in sys.modules
            if m.split(".")[0] in {"presidio_analyzer", "presidio_anonymizer", "spacy"}
        )
        print("LOADED=" + ",".join(loaded))
        """
    )
    result = subprocess.run(  # noqa: S603 - our own interpreter and code
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    [loaded] = [x for x in result.stdout.splitlines() if x.startswith("LOADED=")]
    assert loaded == "LOADED=", loaded
