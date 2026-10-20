"""call.ended gates on the recording via a bounded poll of recording_status.

It must return as soon as the status is terminal (none/completed/failed) and must
NOT block forever if the recording is stuck.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import turncall.services.call_analysis_trigger as trig
from turncall.storage.repositories import call_repo

CALL_ID = "00000000-0000-0000-0000-000000000001"


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _factory() -> _FakeSession:
    return _FakeSession()


def _call(status: str, url: str | None, started: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        recording_status=status,
        recording_url=url,
        started_at=datetime.now(UTC) if started else None,
    )


@pytest.mark.asyncio
async def test_returns_immediately_when_completed() -> None:
    call = _call("completed", "/storage/recordings/x.wav")
    with patch.object(call_repo, "get_call_by_id", AsyncMock(return_value=call)):
        url, status = await trig._wait_for_recording(_factory, CALL_ID)
    assert (url, status) == ("/storage/recordings/x.wav", "completed")


@pytest.mark.asyncio
async def test_polls_until_terminal() -> None:
    # in_progress, in_progress, then completed.
    seq = [
        _call("in_progress", None),
        _call("in_progress", None),
        _call("completed", "/r.wav"),
    ]
    with (
        patch.object(trig, "RECORDING_POLL_INTERVAL_S", 0.01),
        patch.object(call_repo, "get_call_by_id", AsyncMock(side_effect=seq)),
    ):
        url, status = await trig._wait_for_recording(_factory, CALL_ID)
    assert status == "completed" and url == "/r.wav"


@pytest.mark.asyncio
async def test_times_out_without_blocking_forever() -> None:
    stuck = _call("in_progress", None)
    with (
        patch.object(trig, "RECORDING_WAIT_TIMEOUT_S", 0.05),
        patch.object(trig, "RECORDING_POLL_INTERVAL_S", 0.01),
        patch.object(call_repo, "get_call_by_id", AsyncMock(return_value=stuck)),
    ):
        url, status = await trig._wait_for_recording(_factory, CALL_ID)
    # Fires anyway (call.ended must not be lost), reporting the non-terminal state.
    assert status == "in_progress" and url is None


@pytest.mark.asyncio
async def test_a_call_that_never_started_has_nothing_to_wait_for() -> None:
    """A pipeline that failed to build records nothing; call.ended should not
    sit out the whole timeout for it."""
    never = _call("none", None, started=False)
    with (
        patch.object(trig, "RECORDING_WAIT_TIMEOUT_S", 60),
        patch.object(call_repo, "get_call_by_id", AsyncMock(return_value=never)),
    ):
        url, status = await asyncio.wait_for(
            trig._wait_for_recording(_factory, CALL_ID), 1
        )
    assert (url, status) == (None, "none")


@pytest.mark.asyncio
async def test_a_pipeline_cancelled_before_it_started_has_nothing_to_wait_for() -> None:
    """The caller hung up while the services connected: the recorder never saw
    the pipeline start, so `none` is all there will ever be (#367)."""
    never = _call("none", None, started=True)
    with (
        patch.object(trig, "RECORDING_WAIT_TIMEOUT_S", 60),
        patch.object(call_repo, "get_call_by_id", AsyncMock(return_value=never)),
    ):
        url, status = await asyncio.wait_for(
            trig._wait_for_recording(_factory, CALL_ID), 1
        )
    assert (url, status) == (None, "none")


if __name__ == "__main__":
    asyncio.run(test_returns_immediately_when_completed())
    asyncio.run(test_polls_until_terminal())
    asyncio.run(test_times_out_without_blocking_forever())
    print("ok")
