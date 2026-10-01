"""Project deletion (ADR-0011).

Soft-deletes a project after clearing the external side effects the DB cascade
can't reach: Twilio bindings (auto-unbind, owns the failure) and object-storage
blobs (best-effort sweep). The project row + children stay for history; auth
rejects the project's keys once deleted, and a purge job hard-deletes later.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.adapters.storage import create_storage_adapter, delete_objects
from turncall.config.settings import Settings
from turncall.services.recording_storage import recording_storage_key
from turncall.storage.models import CallRow, DocumentRow, KnowledgeBaseRow, utc_now
from turncall.storage.repositories import (
    ambience_repo,
    phone_number_repo,
    project_repo,
)


async def _clear_twilio_number(settings: Settings, number: Any) -> None:
    """Point a Twilio number away from TurnCall, on its own Twilio account if it
    has one. NOT best-effort — a failure propagates so the delete aborts before
    anything is soft-deleted."""
    from turncall.services.twilio_accounts import account_for_number

    account = account_for_number(number, settings)
    if account is None:
        return
    adapter = account.adapter()
    sid = number.external_number_sid
    await adapter.configure_number_webhook(sid, voice_url="", status_callback_url="")
    await adapter.configure_sms_webhook(sid, sms_url="")


async def _sweep_storage(
    session: AsyncSession, settings: Settings, project_id: UUID
) -> None:
    """Delete the project's KB document files + call recordings from object
    storage. Best-effort — an orphaned blob is logged, never fatal."""
    storage = create_storage_adapter(
        settings.storage.backend,
        local_path=settings.storage.local_path,
        s3_bucket=settings.storage.s3_bucket,
        aws_region=settings.storage.aws_region,
    )
    doc_keys = (
        (
            await session.execute(
                select(DocumentRow.storage_key)
                .join(
                    KnowledgeBaseRow,
                    DocumentRow.knowledge_base_id == KnowledgeBaseRow.id,
                )
                .where(KnowledgeBaseRow.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    # The recorder writes recordings/{call_id}.wav; Twilio's callback writes
    # recordings/{call_id}/{sid}.wav and overwrites recording_url, which can
    # then hide the first. Sweep both (#168).
    recordings = (
        await session.execute(
            select(CallRow.id, CallRow.recording_url).where(
                CallRow.project_id == project_id,
                CallRow.recording_url.is_not(None),
            )
        )
    ).all()

    # Ambience renditions (ADR-0021): the rows go with the purge's cascade, and
    # nothing else would ever name these objects again.
    ambience_keys = await ambience_repo.rendition_keys_for_project(session, project_id)

    rec_keys = {f"recordings/{cid}.wav" for cid, _ in recordings} | {
        recording_storage_key(url) for _, url in recordings
    }
    await delete_objects(
        storage,
        [k for k in doc_keys if k] + [k for k in rec_keys if k] + ambience_keys,
    )


async def delete_project(
    session: AsyncSession, settings: Settings, project_id: UUID
) -> None:
    """Auto-unbind the project's Twilio numbers, sweep its storage, then
    soft-delete. Unbind runs FIRST and is not swallowed — if it raises, nothing
    is soft-deleted, so the caller retries rather than stranding a live number.
    Idempotent: re-clearing a webhook / re-deleting a blob is harmless."""
    for number in await phone_number_repo.list_for_project(session, project_id):
        await _clear_twilio_number(settings, number)

    await _sweep_storage(session, settings, project_id)
    await project_repo.soft_delete_project(session, project_id)


async def purge_soft_deleted_projects(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    retention_days: int,
) -> int:
    """Hard-delete projects soft-deleted longer than retention_days ago — the FK
    cascade reclaims each tree. Sweeps storage again first (belt-and-suspenders:
    the soft-delete already swept, but a project soft-deleted by any other path
    is still cleaned). Returns the number purged. retention_days <= 0 disables."""
    if retention_days <= 0:
        return 0

    cutoff = utc_now() - timedelta(days=retention_days)
    async with session_factory() as session:
        project_ids = await project_repo.list_purgeable_project_ids(session, cutoff)
        for project_id in project_ids:
            await _sweep_storage(session, settings, project_id)
            await project_repo.hard_delete_project(session, project_id)
        await session.commit()
    return len(project_ids)
