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
        patch.object(pd.phone_number_repo, "list_for_project", new=AsyncMock(return_value=numbers)),
        patch.object(pd, "_clear_twilio_number", new=AsyncMock()) as clear,
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
        patch.object(pd.phone_number_repo, "list_for_project", new=AsyncMock(return_value=numbers)),
        patch.object(
            pd, "_clear_twilio_number", new=AsyncMock(side_effect=RuntimeError("twilio down"))
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
async def test_no_twilio_creds_skips_unbind():
    pid = uuid.uuid4()
    with (
        patch.object(pd.phone_number_repo, "list_for_project", new=AsyncMock()) as lst,
        patch.object(pd, "_clear_twilio_number", new=AsyncMock()) as clear,
        patch.object(pd, "_sweep_storage", new=AsyncMock()),
        patch.object(pd.project_repo, "soft_delete_project", new=AsyncMock()) as soft,
    ):
        await pd.delete_project(AsyncMock(), _settings(twilio=False), pid)

    lst.assert_not_called()
    clear.assert_not_called()
    soft.assert_awaited_once()  # still soft-deletes


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
    with patch.object(pd.project_repo, "list_purgeable_project_ids", new=AsyncMock()) as lst:
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
            pd.project_repo, "list_purgeable_project_ids", new=AsyncMock(return_value=ids)
        ),
        patch.object(pd, "_sweep_storage", new=AsyncMock()) as sweep,
        patch.object(pd.project_repo, "hard_delete_project", new=AsyncMock()) as hard,
    ):
        n = await pd.purge_soft_deleted_projects(factory, _settings(), retention_days=30)
    assert n == 2
    assert hard.await_count == 2  # each aged project hard-deleted
    assert sweep.await_count == 2  # belt-and-suspenders storage sweep
    session.commit.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_purge_noop_when_nothing_aged():
    factory, _session = _session_factory()
    with (
        patch.object(pd.project_repo, "list_purgeable_project_ids", new=AsyncMock(return_value=[])),
        patch.object(pd.project_repo, "hard_delete_project", new=AsyncMock()) as hard,
    ):
        n = await pd.purge_soft_deleted_projects(factory, _settings(), retention_days=30)
    assert n == 0
    hard.assert_not_called()
