"""Ambience sound endpoints (ADR-0021, #167).

A project uploads a sound once and any of its agents names it by id in
`ambience.sound`. The upload is normalized here — decoded, forced to mono and
rendered once per voice transport rate — because the mixer plays nothing, and
says nothing, for audio at any other shape.
"""

import asyncio
from collections.abc import Iterable
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Response, UploadFile
from loguru import logger

from turncall.adapters.storage import ObjectStorageAdapter, create_storage_adapter
from turncall.api.deps import DbSession
from turncall.api.errors import ApiError, ConflictError, ErrorCode, NotFoundError
from turncall.api.responses import ok
from turncall.api.v1.schemas.ambience import AmbienceSoundResponse
from turncall.auth import Auth, WriteAuth
from turncall.config.settings import get_settings
from turncall.orchestrator.transport_factory import VOICE_OUTPUT_RATES
from turncall.services.ambience_ingest import (
    AudioTooLongError,
    UnreadableAudioError,
    ingest_sound,
)
from turncall.storage.repositories import ambience_repo

router = APIRouter(prefix="/ambience-sounds", tags=["ambience"])

# Mirrors the knowledge-base upload, including the two-stage check.
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


def _storage() -> ObjectStorageAdapter:
    s = get_settings().storage
    return create_storage_adapter(
        s.backend,
        local_path=s.local_path,
        s3_bucket=s.s3_bucket,
        aws_region=s.aws_region,
    )


def rendition_key(project_id: UUID, sound_id: UUID, rate: int) -> str:
    return f"ambience/{project_id}/{sound_id}/{rate}.mp3"


@router.post("", status_code=201)
async def upload_sound(
    file: UploadFile, auth: WriteAuth, session: DbSession
) -> dict[str, Any]:
    """Upload a sound. Stored as one mono MP3 per voice transport rate."""
    # Content-Length first, so a flood of large uploads is refused before the
    # body is read; the header can be absent or wrong, so the bytes decide.
    if file.size is not None and file.size > MAX_UPLOAD_SIZE:
        raise ConflictError(
            f"File too large: {file.size} bytes. Maximum: {MAX_UPLOAD_SIZE} bytes."
        )
    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        raise ConflictError(
            f"File too large: {len(data)} bytes. Maximum: {MAX_UPLOAD_SIZE} bytes."
        )

    content_type = file.content_type or "application/octet-stream"
    try:
        sound = await asyncio.to_thread(
            ingest_sound, data, content_type, VOICE_OUTPUT_RATES
        )
    except UnreadableAudioError as exc:
        raise ApiError(
            status_code=415, code=ErrorCode.VALIDATION_ERROR, message=str(exc)
        ) from exc
    except AudioTooLongError as exc:
        raise ConflictError(str(exc)) from exc

    sound_id = uuid4()
    storage = _storage()
    keys = {
        str(rate): rendition_key(auth.project_id, sound_id, rate)
        for rate in sound.renditions
    }
    try:
        for rate, audio in sound.renditions.items():
            await storage.upload(keys[str(rate)], audio, content_type="audio/mpeg")
        row = await ambience_repo.create_sound(
            session,
            sound_id=sound_id,
            project_id=auth.project_id,
            name=file.filename or "ambience",
            duration_ms=sound.duration_ms,
            renditions=keys,
        )
        await session.commit()
    except Exception:
        # No row means nothing can ever name these objects — don't leave them.
        await _delete_objects(storage, keys.values())
        raise
    return ok(AmbienceSoundResponse.from_row(row, warnings=sound.warnings))


@router.get("")
async def list_sounds(auth: Auth, session: DbSession) -> dict[str, Any]:
    rows = await ambience_repo.list_for_project(session, auth.project_id)
    return ok([AmbienceSoundResponse.from_row(r) for r in rows])


@router.get("/{sound_id}")
async def get_sound(sound_id: UUID, auth: Auth, session: DbSession) -> dict[str, Any]:
    row = await ambience_repo.get_by_id(session, sound_id, project_id=auth.project_id)
    if row is None:
        raise NotFoundError("AmbienceSound", str(sound_id))
    storage = _storage()
    urls = {
        rate: await storage.get_presigned_url(key)
        for rate, key in row.renditions.items()
    }
    return ok(AmbienceSoundResponse.from_row(row, rendition_urls=urls))


@router.get("/{sound_id}/renditions/{rate}")
async def get_rendition_audio(
    sound_id: UUID, rate: int, auth: Auth, session: DbSession
) -> Response:
    """The rendition's MP3 itself. `rendition_urls` are file paths on local
    storage, which no browser can open — this plays on every backend."""
    row = await ambience_repo.get_by_id(session, sound_id, project_id=auth.project_id)
    key = row.renditions.get(str(rate)) if row is not None else None
    if key is None:
        raise NotFoundError("AmbienceRendition", f"{sound_id}/{rate}")
    data = await _storage().download(key)
    return Response(content=data, media_type="audio/mpeg")


@router.delete("/{sound_id}")
async def delete_sound(
    sound_id: UUID, auth: WriteAuth, session: DbSession
) -> dict[str, Any]:
    """Delete a sound and its stored renditions. Refused while an agent names it."""
    row = await ambience_repo.get_by_id(session, sound_id, project_id=auth.project_id)
    if row is None:
        raise NotFoundError("AmbienceSound", str(sound_id))
    agents = await ambience_repo.agents_referencing(session, auth.project_id, sound_id)
    if agents:
        named = [
            {"id": str(a.id), "name": a.name, "version": a.version} for a in agents
        ]
        raise ConflictError(
            "Ambience sound is used by "
            + ", ".join(f"{a['name']} v{a['version']}" for a in named)
            + " — remove it from their ambience first",
            details={"agents": named},
        )
    keys = list(row.renditions.values())
    await ambience_repo.delete_sound(session, row)
    await session.commit()
    # Unlike documents (#168), the objects go too: an orphan here is a thing
    # that looks like it should still play.
    await _delete_objects(_storage(), keys)
    return ok({"deleted": True})


async def _delete_objects(storage: ObjectStorageAdapter, keys: Iterable[str]) -> None:
    for key in keys:
        try:
            await storage.delete(key)
        except Exception as exc:
            logger.warning("ambience_storage_orphan key={key}: {err}", key=key, err=exc)
