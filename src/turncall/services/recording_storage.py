"""Download Twilio recordings and store to local/S3.

Downloads the recording WAV from Twilio, uploads to the configured
storage backend, and returns the storage URL.
"""

from __future__ import annotations

from uuid import UUID

from loguru import logger

from turncall.adapters.http_client import get_http_client
from turncall.adapters.storage import create_storage_adapter
from turncall.config.settings import get_settings


async def download_and_store_recording(
    twilio_recording_url: str,
    call_id: UUID,
    recording_sid: str,
    *,
    auth: tuple[str, str] | None = None,
) -> str:
    """Download a recording from Twilio and upload to storage.

    Args:
        twilio_recording_url: Twilio recording URL (without extension).
        call_id: Call ID for the storage key.
        recording_sid: Twilio recording SID for the filename.
        auth: (account_sid, auth_token) of the account that recorded it; the
            platform's TWILIO_* when omitted.

    Returns:
        The storage URL (local path or S3 URI).
    """
    settings = get_settings()

    # Twilio recording URL needs .wav extension and basic auth
    download_url = f"{twilio_recording_url}.wav"
    auth = auth or (settings.twilio.account_sid, settings.twilio.auth_token)

    logger.info(
        "recording_download_start",
        call_id=str(call_id),
        recording_sid=recording_sid,
    )

    client = get_http_client()
    response = await client.get(download_url, auth=auth, timeout=60.0)
    response.raise_for_status()
    audio_data = response.content

    logger.info(
        "recording_downloaded",
        call_id=str(call_id),
        size_bytes=len(audio_data),
    )

    # Upload to storage
    storage = create_storage_adapter(
        backend=settings.storage.backend,
        local_path=settings.storage.local_path,
        s3_bucket=settings.storage.s3_bucket,
        aws_region=settings.storage.aws_region,
    )
    storage_key = f"recordings/{call_id}/{recording_sid}.wav"
    storage_url = await storage.upload(
        storage_key, audio_data, content_type="audio/wav"
    )

    logger.info(
        "recording_stored",
        call_id=str(call_id),
        storage_url=storage_url,
    )

    return storage_url


def recording_storage_key(url: str | None) -> str | None:
    """The storage key inside a stored `recording_url`, or None when nothing
    was stored. Two writers use two shapes — the call recorder's
    `recordings/{call_id}.wav` and Twilio's callback's
    `recordings/{call_id}/{sid}.wav` — so the URL is the only reliable source.
    Parse s3:// properly: a substring search would match inside the bucket name
    ("call-recordings")."""
    if not url or "recordings/" not in url:
        return None
    if url.startswith("s3://"):
        return url.split("/", 3)[3]  # s3://<bucket>/<key>
    return url[url.rindex("recordings/") :]  # local path ends with the key
