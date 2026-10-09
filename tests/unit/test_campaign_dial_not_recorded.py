"""A campaign dial the carrier placed keeps its reservation (#224, #233).

The dialer releases a target's reservation — live-call slot and CPS place —
whenever `_dial` says nothing was dialled. A failure after the carrier placed
the call used to say exactly that, freeing the slot of a ringing phone.
"""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from turncall.campaigns import dialer
from turncall.domain.enums import CampaignTargetStatus
from turncall.services.outbound_dial import DialNotRecorded

pytestmark = pytest.mark.unit

REPO = "turncall.campaigns.dialer.campaign_repo"


def _factory():
    @asynccontextmanager
    async def session():
        yield AsyncMock()

    return session


def _campaign_and_target():
    campaign = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        from_number_id=uuid.uuid4(),
    )
    target = SimpleNamespace(
        id=uuid.uuid4(), agent_id=None, e164_number="+15550000002", variables={}
    )
    return campaign, target


async def test_a_placed_but_unrecorded_dial_counts_as_dialled():
    campaign, target = _campaign_and_target()
    call_id = uuid.uuid4()
    with (
        patch(
            "turncall.campaigns.dialer.place_outbound_call",
            AsyncMock(side_effect=DialNotRecorded(call_id, "CA1")),
        ),
        patch(f"{REPO}.finish_target", AsyncMock()) as finish,
    ):
        dialled = await dialer._dial(
            _factory(), campaign, target, call_id, dialer.system_clock
        )
    assert dialled is True
    assert finish.await_args.args[2] == CampaignTargetStatus.FAILED
    assert "CA1" in finish.await_args.kwargs["error"]


async def test_a_failure_recording_the_target_counts_as_dialled():
    campaign, target = _campaign_and_target()
    call = SimpleNamespace(id=uuid.uuid4(), provider_call_sid="CA2")
    with (
        patch(
            "turncall.campaigns.dialer.place_outbound_call",
            AsyncMock(return_value=call),
        ),
        patch(f"{REPO}.record_dial", AsyncMock(side_effect=ConnectionError("db"))),
        patch(f"{REPO}.finish_target", AsyncMock()) as finish,
    ):
        dialled = await dialer._dial(
            _factory(), campaign, target, uuid.uuid4(), dialer.system_clock
        )
    assert dialled is True
    assert finish.await_args.args[2] == CampaignTargetStatus.FAILED
    assert "CA2" in finish.await_args.kwargs["error"]


async def test_a_refused_dial_is_still_not_dialled():
    campaign, target = _campaign_and_target()
    with (
        patch(
            "turncall.campaigns.dialer.place_outbound_call",
            AsyncMock(side_effect=RuntimeError("carrier said no")),
        ),
        patch(f"{REPO}.finish_target", AsyncMock()),
    ):
        dialled = await dialer._dial(
            _factory(), campaign, target, uuid.uuid4(), dialer.system_clock
        )
    assert dialled is False
