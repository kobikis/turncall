"""Project deletion (ADR-0011).

Soft-deletes a project after clearing the external side effects the DB cascade
can't reach: Twilio bindings (auto-unbind, owns the failure) and object-storage
blobs (best-effort sweep). The project row + children stay for history; auth
rejects the project's keys once deleted, and a purge job hard-deletes later.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.adapters.storage import create_storage_adapter
from turncall.config.settings import Settings
from turncall.storage.models import CallRow, DocumentRow, KnowledgeBaseRow, utc_now
from turncall.storage.repositories import phone_number_repo, project_repo


async def _clear_twilio_number(settings: Settings, sid: str) -> None:
    """Point a Twilio number away from TurnCall. NOT best-effort — a failure
    propagates so the delete aborts before anything is soft-deleted."""
    from turncall.adapters.telephony.twilio_adapter import TwilioAdapter

    adapter = TwilioAdapter(settings.twilio.account_sid, settings.twilio.auth_token)
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
        await session.execute(
            select(DocumentRow.storage_key)
            .join(
                KnowledgeBaseRow,
                DocumentRow.knowledge_base_id == KnowledgeBaseRow.id,
            )
            .where(KnowledgeBaseRow.project_id == project_id)
        )
    ).scalars().all()
    # Recording keys are deterministic (recordings/{call_id}.wav) — no need to
    # parse recording_url back into a storage key.
    rec_ids = (
        await session.execute(
            select(CallRow.id).where(
                CallRow.project_id == project_id,
                CallRow.recording_url.is_not(None),
            )
        )
    ).scalars().all()

    keys = [k for k in doc_keys if k] + [f"recordings/{cid}.wav" for cid in rec_ids]
    for key in keys:
        try:
            await storage.delete(key)
        except Exception as exc:
            logger.warning(
                "project_delete_storage_orphan key={key}: {err}", key=key, err=exc
            )


async def delete_project(
    session: AsyncSession, settings: Settings, project_id: UUID
) -> None:
    """Auto-unbind the project's Twilio numbers, sweep its storage, then
    soft-delete. Unbind runs FIRST and is not swallowed — if it raises, nothing
    is soft-deleted, so the caller retries rather than stranding a live number.
    Idempotent: re-clearing a webhook / re-deleting a blob is harmless."""
    if settings.twilio.account_sid and settings.twilio.auth_token:
        numbers = await phone_number_repo.list_for_project(session, project_id)
        for number in numbers:
            await _clear_twilio_number(settings, number.external_number_sid)

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
