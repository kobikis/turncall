"""Project credentials — provider keys, Fernet-encrypted at rest (ADR-0020).

Nothing here returns a stored value to an API caller: the decrypting reader is
`load_documents`, which only the pipeline build calls.
"""

import json
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.config.settings import Settings
from turncall.storage.models import (
    PhoneNumberRow,
    ProjectCredentialRow,
    ProjectRow,
    utc_now,
)


class CredentialKeyError(RuntimeError):
    """CREDENTIAL_ENCRYPTION_KEY is unset, malformed, or not the one used."""


def _fernet(settings: Settings) -> Fernet:
    key = settings.credentials.encryption_key
    if not key:
        raise CredentialKeyError(
            "CREDENTIAL_ENCRYPTION_KEY is not set, so project credentials "
            'cannot be stored or read. Generate one with `python -c "from '
            "cryptography.fernet import Fernet; print(Fernet.generate_key()"
            '.decode())"` and set it once — rotating it orphans every '
            "stored credential."
        )
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise CredentialKeyError(
            "CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key "
            "(32 url-safe base64-encoded bytes)."
        ) from exc


def encrypt(document: dict[str, Any], settings: Settings) -> str:
    return _fernet(settings).encrypt(json.dumps(document).encode()).decode()


def decrypt(token: str, settings: Settings) -> dict[str, Any]:
    try:
        return dict(json.loads(_fernet(settings).decrypt(token.encode())))
    except InvalidToken as exc:
        raise CredentialKeyError(
            "A project credential cannot be decrypted with the configured "
            "CREDENTIAL_ENCRYPTION_KEY — was the key rotated?"
        ) from exc


async def upsert(
    session: AsyncSession,
    *,
    project_id: UUID,
    provider: str,
    document: dict[str, Any],
    settings: Settings,
) -> ProjectCredentialRow:
    """Write a credential, replacing any the project holds for this provider.

    One statement, so a replace can never half-apply. The row returned is
    what was written even when the session already holds the credential (a
    read before the replace): RETURNING refreshes it rather than handing
    back the stale copy the session has.
    """
    ciphertext = encrypt(document, settings)
    stmt = (
        insert(ProjectCredentialRow)
        .values(project_id=project_id, provider=provider, ciphertext=ciphertext)
        .on_conflict_do_update(
            constraint="uq_project_credentials_project_provider",
            set_={"ciphertext": ciphertext, "updated_at": utc_now()},
        )
        .returning(ProjectCredentialRow)
    )
    result = await session.execute(stmt, execution_options={"populate_existing": True})
    return result.scalar_one()


async def list_for_project(
    session: AsyncSession, project_id: UUID
) -> list[ProjectCredentialRow]:
    result = await session.execute(
        select(ProjectCredentialRow)
        .where(ProjectCredentialRow.project_id == project_id)
        .order_by(ProjectCredentialRow.provider)
    )
    return list(result.scalars().all())


async def get(
    session: AsyncSession, project_id: UUID, provider: str
) -> ProjectCredentialRow | None:
    """The project's credential for one provider account, still sealed."""
    return await session.scalar(
        select(ProjectCredentialRow).where(
            ProjectCredentialRow.project_id == project_id,
            ProjectCredentialRow.provider == provider,
        )
    )


async def providers_for_project(session: AsyncSession, project_id: UUID) -> set[str]:
    result = await session.execute(
        select(ProjectCredentialRow.provider).where(
            ProjectCredentialRow.project_id == project_id
        )
    )
    return set(result.scalars().all())


async def delete_credential(
    session: AsyncSession, project_id: UUID, provider: str
) -> bool:
    result = await session.execute(
        delete(ProjectCredentialRow).where(
            ProjectCredentialRow.project_id == project_id,
            ProjectCredentialRow.provider == provider,
        )
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def load_documents(
    session: AsyncSession, project_id: UUID, settings: Settings
) -> dict[str, dict[str, Any]]:
    """Every credential the project holds, decrypted: provider -> document."""
    rows = await list_for_project(session, project_id)
    return {row.provider: decrypt(row.ciphertext, settings) for row in rows}


async def documents_for_provider(
    session: AsyncSession, provider: str, settings: Settings
) -> dict[UUID, dict[str, Any]]:
    """One provider's credential in every live project: project -> document.

    For the work that runs for every project at once rather than inside one —
    the Asterisk edge connects to each project's PBX (#209).
    """
    result = await session.execute(
        select(ProjectCredentialRow)
        .join(ProjectRow, ProjectRow.id == ProjectCredentialRow.project_id)
        .where(
            ProjectCredentialRow.provider == provider,
            ProjectRow.deleted_at.is_(None),
        )
    )
    return {
        row.project_id: decrypt(row.ciphertext, settings)
        for row in result.scalars().all()
    }


async def ensure_readable(session: AsyncSession, settings: Settings) -> None:
    """Startup check: fail closed when credentials exist and the key is unusable.

    Raises CredentialKeyError rather than letting the first call on a project
    with a credential discover it with a caller on the line.
    """
    if settings.credentials.encryption_key:
        _fernet(settings)  # malformed is an error whether or not rows exist
    # Phone numbers' own Twilio tokens are sealed with the same key.
    for column in (
        ProjectCredentialRow.ciphertext,
        PhoneNumberRow.twilio_auth_token_ciphertext,
    ):
        first = await session.scalar(select(column).where(column.is_not(None)).limit(1))
        if first is not None:
            decrypt(first, settings)
