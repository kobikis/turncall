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
