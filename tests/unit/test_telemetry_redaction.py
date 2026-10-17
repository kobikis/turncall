"""Redaction in logs and spans (#314, ADR-0024).

What the regular expressions find and leave alone, that a log record is
redacted only inside a redacting project's context (and in the tasks that
context starts), through an `enqueue=True` sink and the stdlib bridge, with an
exception's traceback rendered without its locals, and that a span started
there is redacted on its way to the exporter. End to end, through a call and
a text conversation: `test_harness_log_redaction.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import uuid4

import pytest
from loguru import logger

from turncall.services import redaction, telemetry_redaction
from turncall.services.redaction import DEFAULT_ENTITIES, RedactionPolicy
from turncall.services.telemetry_redaction import redact_line

pytestmark = pytest.mark.unit

ALL = DEFAULT_ENTITIES
POLICY = RedactionPolicy(ALL)
PHONE = "PHONE_NUMBER"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("from +15550001234", "from <PHONE_NUMBER>"),
        ("to whatsapp:+447700900123", "to whatsapp:<PHONE_NUMBER>"),
        ("my number is 415 555 0132.", "my number is <PHONE_NUMBER>."),
        ("(415) 555-0132", "<PHONE_NUMBER>"),
        ("+1 (415) 555-0132", "<PHONE_NUMBER>"),
        ("415.555.0132", "<PHONE_NUMBER>"),
        ("wa_id 15559876543", "wa_id <PHONE_NUMBER>"),
        ("mail jane.doe@example.com now", "mail <EMAIL_ADDRESS> now"),
        ("card 4111 1111 1111 1111", "card <CREDIT_CARD>"),
        ("ssn 536-22-8726", "ssn <US_SSN>"),
        ("iban GB82 WEST 1234 5698 7654 32", "iban <IBAN_CODE>"),
        ("GB82WEST12345698765432", "<IBAN_CODE>"),
    ],
)
def test_what_is_found(text: str, expected: str) -> None:
    assert redact_line(text, ALL) == expected


@pytest.mark.parametrize(
    "text",
    [
        "call 3f1e2d4c-1234-5678-9abc-def012345678",
        "at 2026-10-17T12:34:56.123Z and 2026-10-17 12:34:56+00:00",
        "on 2026-10-17",
        "from 127.0.0.1:54321 and 192.168.100.200",
        "took 12345678.9ms, 0.123456789 of it waiting",
        "duration_ms=1234567 port 8000 v1.2.3",
        "limit 10 20 30 40",
        "sid CA1234567890abcdef1234567890abcdef",
        "card 4111-1111-1111-1112",  # fails Luhn
        "GB00 WEST 1234 5698 7654 32",  # fails the IBAN check
    ],
)
def test_what_is_left_alone(text: str) -> None:
    assert redact_line(text, ALL) == text


def test_only_the_chosen_types() -> None:
    text = "+15550001234 or jane@example.com"
    assert redact_line(text, ("EMAIL_ADDRESS",)) == "+15550001234 or <EMAIL_ADDRESS>"
    assert redact_line(text, ("PERSON",)) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Too many digits for one number: each is found on its own.
        (
            "numbers 4155550132 4155550133",
            "numbers <PHONE_NUMBER> <PHONE_NUMBER>",
        ),
        ("415-555-0132 415-555-0133", "<PHONE_NUMBER> <PHONE_NUMBER>"),
        ("+14155550132 +14155550133", "<PHONE_NUMBER> <PHONE_NUMBER>"),
        # The longest number that fits is taken: a digit too many redacted,
        # rather than one left.
        ("call +44 20 7946 0958 123 456", "call <PHONE_NUMBER> 456"),
        ("at 1760000000123 from 4155550132", "at 1760000000123 from <PHONE_NUMBER>"),
        ("1760000000123 4155550132", "1760000000123 <PHONE_NUMBER>"),
    ],
)
def test_separate_numbers_stay_separate(text: str, expected: str) -> None:
    assert redact_line(text, (PHONE,)) == expected


def test_separate_cards_stay_separate() -> None:
    assert redact_line("4111111111111111 4012888888881881", ("CREDIT_CARD",)) == (
        "<CREDIT_CARD> <CREDIT_CARD>"
    )


@pytest.mark.parametrize(
    ("text", "chosen", "expected"),
    [
        # A WhatsApp JID is email-shaped: its number is found without EMAIL.
        ("+14155550132@s.whatsapp.net", (PHONE,), "<PHONE_NUMBER>@s.whatsapp.net"),
        (
            "jid 14155550132@s.whatsapp.net",
            (PHONE,),
            "jid <PHONE_NUMBER>@s.whatsapp.net",
        ),
        ("+14155550132@s.whatsapp.net", ALL, "<EMAIL_ADDRESS>"),
        (
            "sip:+14155550132@pbx.example.com",
            (PHONE,),
            "sip:<PHONE_NUMBER>@pbx.example.com",
        ),
        # A valid IBAN not chosen is left whole, not mined for a phone.
        ("GB82 WEST 1234 5698 7654 32", (PHONE,), "GB82 WEST 1234 5698 7654 32"),
    ],
)
def test_a_number_inside_another_shape(
    text: str, chosen: tuple[str, ...], expected: str
) -> None:
    assert redact_line(text, chosen) == expected


@pytest.mark.parametrize(
    "text",
    [
        "ts 1760000000123",  # epoch milliseconds
        "ts 1760000000",  # epoch seconds
        "at 1760000000123453",  # epoch microseconds, passes Luhn
        "at 1760000000123456789",  # epoch nanoseconds
        "snowflake 1234567890123456789",
        "id 0412345678901233",  # zero-padded, passes Luhn
    ],
)
def test_timestamps_and_long_ids_are_left_alone(text: str) -> None:
    assert redact_line(text, ALL) == text


def test_a_bare_number_starting_with_1_is_a_phone_at_eleven_digits() -> None:
    # +1's country code, written without its `+` (a WhatsApp id).
    assert redact_line("wa 14155550132", (PHONE,)) == "wa <PHONE_NUMBER>"
    assert redact_line("cn 13812345678", (PHONE,)) == "cn <PHONE_NUMBER>"


def test_a_number_with_a_plus_is_a_phone_even_when_it_passes_luhn() -> None:
    # 14 digits passing Luhn: written as a phone number, it is one.
    assert redact_line("+49170123456786", ("PHONE_NUMBER", "CREDIT_CARD")) == (
        "<PHONE_NUMBER>"
    )


# --- Logs -------------------------------------------------------------------


def _records(lines: list[str]) -> list[dict]:
    return [json.loads(line)["record"] for line in lines]


@pytest.fixture
def logged():
    """What a production-like sink receives: JSON, through loguru's queue."""
    lines: list[str] = []
    sink = logger.add(lines.append, level="DEBUG", serialize=True, enqueue=True)

    async def read() -> list[dict]:
        await logger.complete()
        return _records(lines)

    yield read
    logger.remove(sink)


@pytest.fixture
def redacting(monkeypatch):
    """Every project's policy: the default types."""

    async def policy(_project_id, _sessions=None):
        return POLICY

    monkeypatch.setattr(redaction, "_read_policy_alone", policy)


async def test_a_record_is_redacted_inside_a_redacting_projects_context(
    logged, redacting
) -> None:
    async def call() -> None:
        await telemetry_redaction.for_project(uuid4())
        logger.info(
            "inbound {who}",
            who="+15550001234",
            from_number="+15550001234",
            nested={"said": ["mail jane@example.com"], "count": 3},
            call_id=str(uuid4()),
        )

        async def tool() -> None:  # a task the call starts
            logger.info("tool_called", args={"phone": "415 555 0132"})

        await asyncio.create_task(tool())

    async def elsewhere() -> None:
        logger.info("startup", number="+15550001234")

    await asyncio.gather(call(), elsewhere())
    records = {r["message"]: r for r in await logged()}

    inbound = records["inbound <PHONE_NUMBER>"]["extra"]
    assert inbound["from_number"] == "<PHONE_NUMBER>"
    assert inbound["who"] == "<PHONE_NUMBER>"
    assert inbound["nested"] == {"said": ["mail <EMAIL_ADDRESS>"], "count": 3}
    assert "<" not in inbound["call_id"]
    assert records["tool_called"]["extra"]["args"] == {"phone": "<PHONE_NUMBER>"}
    # Another task, outside any project: as it was.
    assert records["startup"]["extra"]["number"] == "+15550001234"


async def test_a_project_with_redaction_off_logs_as_before(logged) -> None:
    # The unit conftest reads every project's policy as off.
    assert await telemetry_redaction.for_project(uuid4()) is None
    logger.info("inbound", from_number="+15550001234")

    [record] = [r for r in await logged() if r["message"] == "inbound"]
    assert record["extra"]["from_number"] == "+15550001234"


async def test_an_unreadable_policy_redacts_the_default_types(
    logged, monkeypatch
) -> None:
    async def broken(_project_id, _sessions=None):
        raise RuntimeError("database down")

    monkeypatch.setattr(redaction, "_read_policy_alone", broken)
    assert await telemetry_redaction.for_project(uuid4()) == POLICY
    logger.info("inbound", from_number="+15550001234")

    [record] = [r for r in await logged() if r["message"] == "inbound"]
    assert record["extra"]["from_number"] == "<PHONE_NUMBER>"


async def test_a_scope_ends_with_its_block(logged, redacting) -> None:
    async with telemetry_redaction.scoped_to_project(uuid4()) as policy:
        assert policy == POLICY
        logger.info("inside", number="+15550001234")
    logger.info("after", number="+15550001234")

    records = {r["message"]: r["extra"] for r in await logged()}
    assert records["inside"]["number"] == "<PHONE_NUMBER>"
    assert records["after"]["number"] == "+15550001234"


async def test_an_exception_is_rendered_without_its_locals(logged) -> None:
    telemetry_redaction.use(POLICY)
    transcript = "my number is 415 555 0132"  # a local loguru's diagnose shows
    try:
        raise ValueError(f"no slot for {transcript}")
    except ValueError:
        logger.exception("booking_failed")

    [record] = [r for r in await logged() if r["message"].startswith("booking")]
    assert record["exception"] is None
    message = record["message"]
    assert "Traceback" in message and "ValueError" in message
    assert "<PHONE_NUMBER>" in message and "415 555 0132" not in message


async def test_stdlib_records_through_the_bridge_are_redacted(
    logged, monkeypatch
) -> None:
    from turncall.config.logging import setup_logging

    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", list(root.handlers))
    monkeypatch.setattr(root, "level", root.level)
    monkeypatch.setattr(logger, "remove", lambda *_args: None)  # keep `logged`
    monkeypatch.setattr(logger, "add", lambda *_args, **_kwargs: 0)
    setup_logging()

    telemetry_redaction.use(POLICY)
    logging.getLogger("some.library").warning("dialling %s", "+15550001234")

    messages = [r["message"] for r in await logged()]
    assert "dialling <PHONE_NUMBER>" in messages


# --- Spans ------------------------------------------------------------------


@pytest.fixture
def spans():
    """A tracer whose spans reach an in-memory exporter the way the app's
    reach OTLP: marked at start, redacted by the wrapping exporter."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exported = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(telemetry_redaction.RedactingSpanExporter(exported))
    )
    telemetry_redaction.install_span_redaction(provider)
    return provider.get_tracer("test"), exported


async def test_a_span_started_in_a_redacting_projects_context_is_redacted(
    spans,
) -> None:
    from opentelemetry.trace import Status, StatusCode

    tracer, exported = spans

    async def call() -> None:
        telemetry_redaction.use(POLICY)
        with tracer.start_as_current_span("stt") as span:
            span.set_attribute("transcript", "my number is 415 555 0132")
            span.set_attribute("input", ["mail jane@example.com", "plain"])
            span.set_attribute("metrics.ttfb", 0.25)
            span.add_event("said", {"text": "+15550001234"})
            span.set_status(Status(StatusCode.ERROR, "no slot for +15550001234"))

    async def elsewhere() -> None:
        with tracer.start_as_current_span("startup") as span:
            span.set_attribute("transcript", "my number is 415 555 0132")

    await asyncio.gather(call(), elsewhere())
    by_name = {s.name: s for s in exported.get_finished_spans()}

    stt = by_name["stt"]
    assert stt.attributes["transcript"] == "my number is <PHONE_NUMBER>"
    assert stt.attributes["input"] == ("mail <EMAIL_ADDRESS>", "plain")
    assert stt.attributes["metrics.ttfb"] == 0.25
    assert stt.attributes["turncall.redaction"] == ALL
    assert stt.events[0].attributes["text"] == "<PHONE_NUMBER>"
    assert stt.status.description == "no slot for <PHONE_NUMBER>"
    assert by_name["startup"].attributes["transcript"] == "my number is 415 555 0132"


async def test_a_redacting_projects_numbers_stay_off_the_calls_spans(
    monkeypatch,
) -> None:
    """`PIPECAT_TRACE_INCLUDE_PII` on (the default), the call's numbers go on
    its spans unless its project redacts."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from turncall.orchestrator.call_session import CallSession
    from turncall.storage.repositories import call_repo

    @asynccontextmanager
    async def sessions():
        yield None

    async def the_call(_session, _call_id):
        return SimpleNamespace(
            direction="inbound",
            provider="twilio",
            from_number="+15550001234",
            to_number="+15550009999",
        )

    monkeypatch.setattr(call_repo, "get_call_by_id", the_call)
    session = CallSession.__new__(CallSession)
    session._call_context = SimpleNamespace(
        project_id=uuid4(),
        agent_id=uuid4(),
        call_id=uuid4(),
        call_sid="CA1",
        records=True,
        session_factory=sessions,
    )

    _, attrs = await session._build_telemetry()
    assert attrs["turncall.from_number"] == "+15550001234"
    telemetry_redaction.use(POLICY)
    _, attrs = await session._build_telemetry()
    assert "turncall.from_number" not in attrs
    assert "turncall.to_number" not in attrs
