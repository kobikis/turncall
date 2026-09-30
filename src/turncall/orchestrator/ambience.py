"""A call's ambience mixer, or None (ADR-0021).

The one rule that matters: the sound id resolves against **the call's**
project, never against anything the config says about itself. For a stored
agent that was already checked at save; for an inline agent (ADR-0017) the id
arrived from a call-init webhook and has passed through no project check at
all, and playing another project's recording sounds exactly like the feature
working (#173).

Ambience can never end a call: every miss below warns and returns None, and the
call runs in a silent room.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from loguru import logger

from turncall.adapters.storage import create_storage_adapter
from turncall.domain.models import AgentConfig
from turncall.storage.repositories import ambience_repo

# A rendition is ~70-130 KB; past this the call starts without its room.
FETCH_TIMEOUT_SECONDS = 5.0


async def build_ambience_mixer(
    config: AgentConfig,
    *,
    project_id: UUID,
    sample_rate: int,
    session_factory: Any,
    settings: Any,
) -> Any | None:
    """The transport's `audio_out_mixer` for this call, or None for none."""
    ambience = config.ambience
    if ambience is None:
        return None
    try:
        path = await _rendition_path(
            ambience.sound,
            project_id=project_id,
            sample_rate=sample_rate,
            session_factory=session_factory,
            storage_settings=settings.storage,
        )
    except Exception as exc:
        logger.warning(
            "ambience_skipped: fetching sound {sound} failed, call runs without "
            "ambience: {err}",
            sound=str(ambience.sound),
            err=exc,
        )
        return None
    if path is None:
        return None

    from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer

    return SoundfileMixer(
        sound_files={"ambience": str(path)},
        default_sound="ambience",
        volume=ambience.volume,
    )


async def _rendition_path(
    sound: UUID,
    *,
    project_id: UUID,
    sample_rate: int,
    session_factory: Any,
    storage_settings: Any,
) -> Path | None:
    async with session_factory() as session:
        row = await ambience_repo.get_by_id(session, sound, project_id=project_id)
    if row is None:
        logger.warning(
            "ambience_skipped: sound {sound} is not an ambience sound in project "
            "{project}, call runs without ambience",
            sound=str(sound),
            project=str(project_id),
        )
        return None
    key = row.renditions.get(str(sample_rate))
    if key is None:
        logger.warning(
            "ambience_skipped: sound {sound} has no {rate} Hz rendition, call runs "
            "without ambience",
            sound=str(sound),
            rate=sample_rate,
        )
        return None

    # ponytail: no eviction — bounded by the number of distinct sounds, and a
    # sound is immutable so a cached rendition never goes stale. Add an LRU if a
    # deployment ever holds enough sounds for the disk to notice.
    path = Path(storage_settings.ambience_cache_dir) / str(sound) / f"{sample_rate}.mp3"
    if not path.exists():
        storage = create_storage_adapter(
            storage_settings.backend,
            local_path=storage_settings.local_path,
            s3_bucket=storage_settings.s3_bucket,
            aws_region=storage_settings.aws_region,
        )
        # Bounded: on WebRTC and WhatsApp this runs before the SDP answer, so a
        # hung fetch is a caller hearing nothing at all.
        data = await asyncio.wait_for(storage.download(key), FETCH_TIMEOUT_SECONDS)
        await asyncio.to_thread(_write_atomically, path, data)
    return path


def _write_atomically(path: Path, data: bytes) -> None:
    """Two calls fetching the same sound at once must never read half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
