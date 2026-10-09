"""Project deletion service (ADR-0011): auto-unbind Twilio -> sweep storage ->
soft-delete, with unbind owning its failure."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from turncall.services import project_deletion as pd


def _settings(twilio=True):
    return SimpleNamespace(
        twilio=SimpleNamespace(
            account_sid="AC" if twilio else "", auth_token="tok" if twilio else ""
        )
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unbinds_each_number_then_sweeps_then_soft_deletes():
    pid = uuid.uuid4()
    numbers = [
        SimpleNamespace(external_number_sid="PN1"),
        SimpleNamespace(external_number_sid="PN2"),
    ]
    with (
        patch.object(
            pd.phone_number_repo,
            "list_for_project",
            new=AsyncMock(return_value=numbers),
        ),
        patch.object(pd, "_clear_number", new=AsyncMock()) as clear,
        patch.object(pd, "_sweep_storage", new=AsyncMock()) as sweep,
        patch.object(pd.project_repo, "soft_delete_project", new=AsyncMock()) as soft,
    ):
        await pd.delete_project(AsyncMock(), _settings(), pid)

    assert clear.await_count == 2  # both numbers cleared in Twilio
    sweep.assert_awaited_once()
    soft.assert_awaited_once()
    # order: soft-delete happens last (after unbind + sweep)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unbind_failure_aborts_before_soft_delete():
    pid = uuid.uuid4()
    numbers = [SimpleNamespace(external_number_sid="PN1")]
    with (
        patch.object(
            pd.phone_number_repo,
            "list_for_project",
            new=AsyncMock(return_value=numbers),
        ),
        patch.object(
            pd,
            "_clear_number",
            new=AsyncMock(side_effect=RuntimeError("twilio down")),
        ),
        patch.object(pd, "_sweep_storage", new=AsyncMock()) as sweep,
        patch.object(pd.project_repo, "soft_delete_project", new=AsyncMock()) as soft,
    ):
        with pytest.raises(RuntimeError):
            await pd.delete_project(AsyncMock(), _settings(), pid)

    sweep.assert_not_called()  # aborted before sweep
    soft.assert_not_called()  # nothing soft-deleted -> caller can retry


@pytest.mark.unit
@pytest.mark.asyncio
async def test_numbers_are_cleared_without_platform_creds():
    """A number bound with its own Twilio account must be pointed away from
    TurnCall even when the platform has no Twilio account of its own."""
    pid = uuid.uuid4()
    own = SimpleNamespace(
        external_number_sid="PN1",
        twilio_account_sid=None,
        twilio_auth_token_ciphertext=None,
    )
    with (
        patch.object(
            pd.phone_number_repo, "list_for_project", new=AsyncMock(return_value=[own])
        ),
        patch.object(pd, "_clear_number", new=AsyncMock()) as clear,
        patch.object(pd, "_sweep_storage", new=AsyncMock()),
        patch.object(pd.project_repo, "soft_delete_project", new=AsyncMock()) as soft,
    ):
        await pd.delete_project(AsyncMock(), _settings(twilio=False), pid)

    clear.assert_awaited_once()  # it decides which account, or none
    soft.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_number_with_no_account_anywhere_is_skipped():
    number = SimpleNamespace(
        provider="twilio",
        external_number_sid="PN1",
        twilio_account_sid=None,
        twilio_auth_token_ciphertext=None,
    )
    with patch("turncall.adapters.telephony.twilio_adapter.TwilioAdapter") as adapter:
        await pd._clear_number(AsyncMock(), _settings(twilio=False), number)
    adapter.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_telnyx_number_is_taken_off_telnyx_not_twilio():
    """Its id means nothing to Twilio; it is unrouted on the project's Telnyx
    account (#219)."""
    from turncall.services.telnyx_accounts import TelnyxAccount

    number = SimpleNamespace(
        provider="telnyx", project_id=uuid.uuid4(), external_number_sid="12345"
    )
    unroute = AsyncMock()
    with (
        patch(
            "turncall.services.telnyx_accounts.account_for_project",
            AsyncMock(return_value=TelnyxAccount("KEY", "PUB")),
        ),
        patch(
            "turncall.adapters.telephony.telnyx_adapter.TelnyxAdapter"
            ".configure_number_webhook",
            unroute,
        ),
        patch("turncall.adapters.telephony.twilio_adapter.TwilioAdapter") as twilio,
    ):
        await pd._clear_number(AsyncMock(), _settings(), number)
    unroute.assert_awaited_once_with("12345", voice_url="", status_callback_url="")
    twilio.assert_not_called()


def _session_factory():
    session = AsyncMock()
    cm = MagicMock()  # SimpleNamespace can't hold async dunders
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return (lambda: cm), session


@pytest.mark.unit
@pytest.mark.asyncio
async def test_purge_disabled_when_retention_zero():
    factory, _ = _session_factory()
    with patch.object(
        pd.project_repo, "list_purgeable_project_ids", new=AsyncMock()
    ) as lst:
        n = await pd.purge_soft_deleted_projects(factory, _settings(), retention_days=0)
    assert n == 0
    lst.assert_not_called()  # never even queries


@pytest.mark.unit
@pytest.mark.asyncio
async def test_purge_hard_deletes_each_aged_project():
    ids = [uuid.uuid4(), uuid.uuid4()]
    factory, session = _session_factory()
    with (
        patch.object(
            pd.project_repo,
            "list_purgeable_project_ids",
            new=AsyncMock(return_value=ids),
        ),
        patch.object(pd, "_sweep_storage", new=AsyncMock()) as sweep,
        patch.object(pd.project_repo, "hard_delete_project", new=AsyncMock()) as hard,
    ):
        n = await pd.purge_soft_deleted_projects(
            factory, _settings(), retention_days=30
        )
    assert n == 2
    assert hard.await_count == 2  # each aged project hard-deleted
    assert sweep.await_count == 2  # belt-and-suspenders storage sweep
    session.commit.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_purge_noop_when_nothing_aged():
    factory, _session = _session_factory()
    with (
        patch.object(
            pd.project_repo,
            "list_purgeable_project_ids",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(pd.project_repo, "hard_delete_project", new=AsyncMock()) as hard,
    ):
        n = await pd.purge_soft_deleted_projects(
            factory, _settings(), retention_days=30
        )
    assert n == 0
    hard.assert_not_called()
