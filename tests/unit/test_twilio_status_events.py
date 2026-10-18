"""The call event each Twilio status callback leaves (#312 review).

Every status but `completed` and `failed` used to go out as `call.started`,
so an outbound call that rang and was never answered told subscribers it had
started at `ringing` and again at `no-answer`. Now none does: the dial sent
`call.started` already, so an answer (`in-progress`) sending it too told an
outbound call's subscribers twice, where Telnyx's answer sends nothing
(#329). A call nobody picked up leaves what Telnyx's hangup leaves: a stored
`call.ended` row, nothing sent. The end-to-end halves are
`test_an_outbound_call` and `test_an_outbound_call_nobody_answers` in
`tests/integration/test_carrier_contract.py`.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from turncall.domain.call_state import TWILIO_STATUS_MAP
from turncall.events import emit
from turncall.webhooks.twilio_handlers import _record_status

pytestmark = pytest.mark.unit

# Twilio's status -> (sent and stored, stored only).
LEAVES = {
    "queued": ([], []),
    "ringing": ([], []),
    "in-progress": ([], []),
    "completed": ([], ["call.ended"]),
    "busy": ([], ["call.ended"]),
    "no-answer": ([], ["call.ended"]),
    "failed": (["call.failed"], []),
    "canceled": (["call.failed"], []),  # a failed dial, as the status map reads it
}


@pytest.mark.parametrize("twilio_status", sorted(LEAVES))
async def test_a_status_leaves_its_own_event(twilio_status, monkeypatch) -> None:
    emitted, recorded = AsyncMock(), AsyncMock()
    monkeypatch.setattr(emit, "emit_call_event", emitted)
    monkeypatch.setattr(emit, "record_call_event", recorded)
    call = SimpleNamespace(
        id=uuid4(), project_id=uuid4(), from_number="+15550001", to_number="+15550002"
    )

    await _record_status(
        AsyncMock(), call, TWILIO_STATUS_MAP[twilio_status], "CA1", twilio_status
    )

    sent = [c.kwargs["event_type"] for c in emitted.await_args_list]
    stored = [c.kwargs["event_type"] for c in recorded.await_args_list]
    assert (sent, stored) == LEAVES[twilio_status]


def test_every_status_twilio_sends_is_listed() -> None:
    assert set(LEAVES) == set(TWILIO_STATUS_MAP)
