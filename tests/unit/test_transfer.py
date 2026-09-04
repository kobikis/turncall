"""Transfer pure logic — intent (de)serialization, briefing normalization,
callback URLs, AMD normalization, and the briefing schema union. See ADR-0009.
"""

from uuid import uuid4

import pytest

from turncall.api.v1.schemas.call_control import BriefingSummary, TransferCallRequest
from turncall.services import transfer as t
from turncall.webhooks.twilio_handlers import _normalize_answered_by

pytestmark = pytest.mark.unit


def test_intent_json_roundtrip():
    intent = t.TransferIntent(
        target_number="+15551234567",
        transfer_mode="warm",
        transfer_message="hold please",
        briefing_text="angry customer",
        briefing_from_summary=False,
        fallback_message="no one home",
    )
    assert t.TransferIntent.from_json(intent.to_json()) == intent


def test_normalize_briefing_string():
    assert t.normalize_briefing("hi there") == ("hi there", False)


def test_normalize_briefing_from_summary_dict():
    assert t.normalize_briefing({"from_summary": True}) == (None, True)


def test_normalize_briefing_pydantic():
    assert t.normalize_briefing(BriefingSummary(from_summary=True)) == (None, True)


def test_normalize_briefing_none():
    assert t.normalize_briefing(None) == (None, False)


def test_callback_urls():
    cid = uuid4()
    urls = t.transfer_callback_urls("https://h.test/", cid)
    assert urls["whisper"] == f"https://h.test/webhooks/twilio/transfer-whisper/{cid}"
    assert urls["result"] == f"https://h.test/webhooks/twilio/transfer-result/{cid}"
    assert urls["amd"] == f"https://h.test/webhooks/twilio/transfer-amd/{cid}"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("human", "human"),
        ("machine_start", "machine"),
        ("machine_end_beep", "machine"),
        ("fax", "fax"),
        ("unknown", "unknown"),
        ("", "unknown"),
    ],
)
def test_normalize_answered_by(raw, expected):
    assert _normalize_answered_by(raw) == expected


def test_schema_briefing_accepts_string_and_dict():
    r1 = TransferCallRequest(target_number="+15551234567", briefing="static text")
    assert r1.briefing == "static text"

    r2 = TransferCallRequest(
        target_number="+15551234567", briefing={"from_summary": True}
    )
    assert isinstance(r2.briefing, BriefingSummary)
    assert r2.briefing.from_summary is True


def test_schema_renamed_field_present():
    r = TransferCallRequest(
        target_number="+15551234567",
        transfer_message="connecting you",
        fallback_message="nobody home",
    )
    assert r.transfer_message == "connecting you"
    assert r.fallback_message == "nobody home"
    assert not hasattr(r, "pre_transfer_message")
