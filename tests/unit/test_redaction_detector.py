"""The real detector: Presidio over spaCy's small model, as shipped (#313).

Loads Presidio (a few seconds, once for the module). It is a dependency of the
engine, model included, so these run wherever the test suite does.
"""

from __future__ import annotations

import pytest

from turncall.services import redaction
from turncall.services.redaction import DEFAULT_ENTITIES, SUPPORTED_ENTITIES

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def detector() -> redaction.Detector:
    return redaction._build_detector()


def test_every_choosable_entity_type_is_one_the_detector_knows(detector) -> None:
    """The API validates against a static list so a request never loads
    Presidio; it must not offer a type the detector would ignore."""
    known = set(detector._analyzer.get_supported_entities("en"))  # type: ignore[attr-defined]

    assert set(DEFAULT_ENTITIES) <= SUPPORTED_ENTITIES
    assert known >= SUPPORTED_ENTITIES, SUPPORTED_ENTITIES - known


def test_a_number_and_an_email_said_on_a_call(detector) -> None:
    said = "Sure, my number is 415 555 0132 and my email is jane.doe@example.com."

    assert detector.redact(said, DEFAULT_ENTITIES) == (
        "Sure, my number is <PHONE_NUMBER> and my email is <EMAIL_ADDRESS>."
    )


def test_only_the_chosen_types_are_replaced(detector) -> None:
    said = "Call 415 555 0132 or write to jane.doe@example.com."

    assert detector.redact(said, ("EMAIL_ADDRESS",)) == (
        "Call 415 555 0132 or write to <EMAIL_ADDRESS>."
    )


def test_redacting_twice_changes_nothing_more(detector) -> None:
    """`call.ended`'s transcript is built from stored, already-redacted rows
    and redacted again on the way out."""
    once = detector.redact(
        "Card 4111 1111 1111 1111, SSN 536-22-8726.", DEFAULT_ENTITIES
    )

    assert "4111" not in once and "8726" not in once
    assert detector.redact(once, DEFAULT_ENTITIES) == once


def test_a_missing_model_is_an_error_not_a_download(monkeypatch) -> None:
    """Presidio pip-installs a missing spaCy model at runtime; mid-call, that
    is a network fetch and a venv write. Refused before it gets the chance."""
    import spacy.cli
    import spacy.util

    downloads: list[str] = []
    monkeypatch.setattr(spacy.util, "is_package", lambda _name: False)
    monkeypatch.setattr(spacy.cli, "download", downloads.append)

    with pytest.raises(redaction.RedactionUnavailable, match="en_core_web_sm"):
        redaction._build_detector()
    assert downloads == []


async def test_the_worker_process_redacts_and_leaves_identifiers_alone() -> None:
    """The shipped path: the model loads in its own worker process, and with
    ORGANIZATION chosen a failover's provider names, read back as data, are
    left as they are while its error text is analysed (#327 review, 9)."""
    from turncall.services.redaction import RedactionPolicy, redact_payload

    policy = RedactionPolicy(("ORGANIZATION", "PHONE_NUMBER", "EMAIL_ADDRESS"))
    try:
        assert await redaction.ready()
        failover = await redact_payload(
            {
                "role": "llm",
                "from": "Microsoft Azure",
                "to": "Google",
                "error": "Microsoft Azure said: call 415 555 0132 for support",
            },
            policy,
            event_type="provider.failover",
        )
        said = await redact_payload(
            {"text": "Write to jane.doe@example.com", "role": "customer"},
            policy,
            event_type="transcript.final",
        )
    finally:
        redaction.shutdown()

    assert failover["from"] == "Microsoft Azure" and failover["to"] == "Google"
    assert "415 555 0132" not in failover["error"]
    assert "<PHONE_NUMBER>" in failover["error"]
    assert said == {"text": "Write to <EMAIL_ADDRESS>", "role": "customer"}


async def test_the_worker_redacts_free_json_whatever_its_keys_are() -> None:
    """A tool's arguments, a takeaway (#314): the keys are the user's, so a
    number under `customer_id` or `callback_number` is found by the detector
    itself, as is text in a plain string."""
    from turncall.services.redaction import RedactionPolicy, redact_json, redact_text

    policy = RedactionPolicy(DEFAULT_ENTITIES)
    try:
        assert await redaction.ready()
        value = await redact_json(
            {
                "customer_id": "4155550132",
                "callback_number": "+44 20 7946 0958",
                "notes": ["write to jane.doe@example.com"],
                "booked_at": "2026-10-16T12:00:00+00:00",
                "party_size": 4,
                # JSON numbers, as a webhook may answer them.
                "card_number": 4111111111111111,
                "phone": 4155550132,
                "sent_at_ms": 1760000000123,
            },
            policy,
        )
        text = await redact_text("my number is 415 555 0132", policy)
    finally:
        redaction.shutdown()

    assert value == {
        "customer_id": "<PHONE_NUMBER>",
        "callback_number": "<PHONE_NUMBER>",
        "notes": ["write to <EMAIL_ADDRESS>"],
        "booked_at": "2026-10-16T12:00:00+00:00",
        "party_size": 4,
        "card_number": "<CREDIT_CARD>",
        "phone": "<PHONE_NUMBER>",
        "sent_at_ms": 1760000000123,
    }
    assert text == "my number is <PHONE_NUMBER>"
