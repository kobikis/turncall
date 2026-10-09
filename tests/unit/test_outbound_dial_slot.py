"""A dial the carrier placed keeps its live-call slot (#224).

`place_outbound_call` freed the slot on any failure, including one in the
bookkeeping *after* the carrier had placed the call. The phone was ringing,
its slot free: another dial could be admitted into it, and once the first
call's media stream re-took its own the project carried one call over its
limit. A slot is now freed only when nothing was dialled; a placed call keeps
it until it ends, or until its lease lapses if nothing ever holds it.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from turncall.services import outbound_dial
from turncall.services.outbound_dial import DialNotRecorded, place_outbound_call

pytestmark = pytest.mark.unit

CALLS = "turncall.storage.repositories.call_repo"
MODULE = "turncall.services.outbound_dial"


@pytest.fixture
def placed(monkeypatch):
    """Everything up to and including the carrier succeeds."""
    state = SimpleNamespace()
    number = SimpleNamespace(
        id=uuid.uuid4(),
        e164_number="+15550000001",
        provider="twilio",
        twilio_account_sid=None,
        twilio_auth_token_ciphertext=None,
    )
    adapter = SimpleNamespace(initiate_outbound_call=AsyncMock(return_value="CA1"))
    settings = SimpleNamespace(server=SimpleNamespace(public_base_url="https://tc"))
    monkeypatch.setattr(f"{MODULE}.get_settings", lambda: settings)
    with (
        patch(f"{MODULE}.suppression_repo.is_suppressed", AsyncMock(return_value=False)),
        patch(
            f"{MODULE}.agent_repo.get_agent_by_id",
            AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
        ),
        patch(f"{MODULE}.phone_number_repo.get_by_id", AsyncMock(return_value=number)),
        patch(
            f"{MODULE}._carrier_account",
            AsyncMock(return_value=SimpleNamespace(adapter=lambda: adapter)),
        ),
        patch(f"{MODULE}.admit_outbound_call", AsyncMock()),
        patch(f"{MODULE}.release_live_call", AsyncMock()) as release,
        patch(
            f"{CALLS}.create_call",
            AsyncMock(side_effect=lambda s, **kw: SimpleNamespace(id=kw["call_id"])),
        ),
        patch(f"{CALLS}.update_call_status", AsyncMock()) as update,
        patch(f"{CALLS}.get_next_sequence_number", AsyncMock(return_value=1)),
        patch(f"{CALLS}.create_call_event", AsyncMock()) as event,
    ):
        state.release, state.update, state.event, state.adapter = (
            release,
            update,
            event,
            adapter,
        )
        yield state


async def _place(**kw):
    return await place_outbound_call(
        AsyncMock(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        from_number_id=uuid.uuid4(),
        to_number="+15550000002",
        metadata={},
        **kw,
    )


async def test_bookkeeping_after_the_dial_fails_and_the_slot_is_kept(placed):
    placed.update.return_value = None  # the row vanished mid-dial
    with pytest.raises(DialNotRecorded) as raised:
        await _place()
    assert placed.adapter.initiate_outbound_call.await_count == 1
    assert raised.value.provider_call_sid == "CA1"
    placed.release.assert_not_awaited()


async def test_a_failing_call_event_keeps_the_slot_too(placed):
    placed.update.side_effect = lambda s, cid, **kw: SimpleNamespace(id=cid)
    placed.event.side_effect = ConnectionError("db went away")
    with pytest.raises(DialNotRecorded):
        await _place()
    placed.release.assert_not_awaited()


async def test_a_carrier_refusal_frees_the_slot(placed):
    placed.adapter.initiate_outbound_call.side_effect = RuntimeError("refused")
    with pytest.raises(RuntimeError):
        await _place()
    placed.release.assert_awaited_once()


async def test_a_refusal_before_the_carrier_frees_the_slot(placed):
    async def before_carrier() -> None:
        raise LookupError("paused")

    with pytest.raises(LookupError):
        await _place(before_carrier=before_carrier)
    placed.adapter.initiate_outbound_call.assert_not_awaited()
    placed.release.assert_awaited_once()


def test_the_error_names_the_placed_call():
    exc = DialNotRecorded(uuid.UUID(int=1), "CA9")
    assert "CA9" in str(exc)
    assert isinstance(exc, outbound_dial.DialNotRecorded)
