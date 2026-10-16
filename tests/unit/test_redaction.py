"""Redaction's rules, on a stand-in detector (#313, ADR-0024).

Which strings of a payload are text, what a phone field becomes, that the
caller's dict is left alone, that a broken detector withholds rather than
leaks, that detection runs off the event loop, and that nothing imports
Presidio until a project with redaction on needs it. The real detector is
`test_redaction_detector.py`; the whole path is the harness's
`test_harness_redaction.py`.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from turncall.api.v1.schemas.projects import ProjectRedaction
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


@pytest.fixture
def detector(monkeypatch) -> Iterator[_Marks]:
    marks = _Marks()
    monkeypatch.setattr(redaction, "_DETECTOR", None)
    monkeypatch.setattr(redaction, "_build_detector", lambda: marks)
    yield marks


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


async def test_a_failing_detector_withholds_the_text(monkeypatch) -> None:
    """Never raw because redaction broke: every text string is withheld, and
    the event still goes out."""

    def broken() -> None:
        raise RuntimeError("model missing")

    monkeypatch.setattr(redaction, "_DETECTOR", None)
    monkeypatch.setattr(redaction, "_build_detector", broken)

    out = await redaction.redact_payload(
        {"text": "hello", "call_id": "kept", "from_number": "+15550001234"}, POLICY
    )

    assert out == {
        "text": "<REDACTED>",
        "call_id": "kept",
        "from_number": "<PHONE_NUMBER>",  # no detector needed for that
    }


async def test_detection_runs_off_the_event_loop(monkeypatch) -> None:
    """A slow detection (a whole transcript takes hundreds of ms) leaves the
    loop free to keep ticking: on the loop it would be dead air on every live
    call in the process (ADR-0004)."""
    slow = _Marks(delay=0.3)
    monkeypatch.setattr(redaction, "_DETECTOR", None)
    monkeypatch.setattr(redaction, "_build_detector", lambda: slow)
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

    assert threading.get_ident() not in slow.threads
    assert ticks >= 10, f"the loop was blocked: {ticks} ticks in 300ms"


async def test_the_detector_is_built_once(monkeypatch) -> None:
    built: list[_Marks] = []

    def build() -> _Marks:
        built.append(_Marks())
        return built[-1]

    monkeypatch.setattr(redaction, "_DETECTOR", None)
    monkeypatch.setattr(redaction, "_build_detector", build)

    await asyncio.gather(
        *(redaction.redact_payload({"text": SECRET}, POLICY) for _ in range(5))
    )

    assert len(built) == 1


def test_off_or_on_with_defaults() -> None:
    assert policy_of(False, ["PERSON"]) is None
    assert policy_of(True, None) == RedactionPolicy(DEFAULT_ENTITIES)
    assert policy_of(True, ["PERSON"]) == RedactionPolicy(("PERSON",))
    assert "PERSON" not in DEFAULT_ENTITIES


def test_the_setting_names_known_entity_types_only() -> None:
    assert ProjectRedaction(enabled=True).entities is None
    assert ProjectRedaction(entities=["PERSON", "PERSON"]).entities == ["PERSON"]
    with pytest.raises(ValidationError, match="unknown entity types"):
        ProjectRedaction(entities=["PHONE", "PERSON"])
    with pytest.raises(ValidationError, match="at least one"):
        ProjectRedaction(entities=[])
    with pytest.raises(ValidationError):
        ProjectRedaction(enabled=True, extra=1)  # type: ignore[call-arg]


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
