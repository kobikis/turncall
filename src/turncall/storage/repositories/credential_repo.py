"""Project credentials — provider keys, Fernet-encrypted at rest (ADR-0020).

Nothing here returns a stored value to an API caller: the decrypting reader is
`load_documents`, which only the pipeline build calls.
"""

import json
from typing import Any
from uuid import UUID

from cryptography.fernet import InvalidToken
from sqlalchemy import Text, cast, delete, select, type_coerce
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.config.settings import Settings
from turncall.domain.config_secrets import map_secrets
from turncall.storage.models import (
    AgentRow,
    PhoneNumberRow,
    ProjectCredentialRow,
    ProjectRow,
    WebhookSubscriptionRow,
    utc_now,
)
from turncall.storage.sealing import PREFIX, fernet, unseal
from turncall.storage.sealing import CredentialKeyError as CredentialKeyError

# The key, its error and the cipher live with the rest of what the key seals
# (#338); every `credential_repo.CredentialKeyError` keeps working through here.


def encrypt(document: dict[str, Any], settings: Settings) -> str:
    return fernet(settings).encrypt(json.dumps(document).encode()).decode()


def decrypt(token: str, settings: Settings) -> dict[str, Any]:
    try:
        return dict(json.loads(fernet(settings).decrypt(token.encode())))
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
    """Startup check: fail closed when secrets exist and the key is unusable.

    Raises CredentialKeyError rather than letting the first call on a project
    with a credential discover it with a caller on the line. One stored value
    per column is opened: the key reads all of them or none.

    The values sealed at rest (#338) are sampled where they are few — webhook
    secrets, numbers' server_url secrets, agent configs. A call's or an eval
    run's inline config is sealed with the same key, so these prove it too
    without a startup scan of the largest tables. Read past the column type,
    so the check sees what is stored whether or not the column unseals.
    """
    if settings.credentials.encryption_key:
        fernet(settings)  # malformed is an error whether or not rows exist
    # Phone numbers' own Twilio tokens are sealed with the same key.
    for column in (
        ProjectCredentialRow.ciphertext,
        PhoneNumberRow.twilio_auth_token_ciphertext,
    ):
        first = await session.scalar(select(column).where(column.is_not(None)).limit(1))
        if first is not None:
            decrypt(first, settings)
    for secret in (
        type_coerce(WebhookSubscriptionRow.secret, Text),
        type_coerce(PhoneNumberRow.server_url_secret, Text),
    ):
        first = await session.scalar(
            select(secret).where(secret.startswith(PREFIX, autoescape=True)).limit(1)
        )
        if first is not None:
            unseal(first, settings)
    blob = cast(AgentRow.config_blob, Text)
    first = await session.scalar(
        select(blob).where(blob.contains(f'"{PREFIX}', autoescape=True)).limit(1)
    )
    if first is not None:
        _unseal_one(json.loads(first), settings)


def _unseal_one(config: dict[str, Any], settings: Settings) -> None:
    """Open the first sealed secret in an agent config, if it holds one."""
    sealed: list[str] = []

    def collect(value: str) -> str:
        if value.startswith(PREFIX):
            sealed.append(value)
        return value

    map_secrets(config, collect)
    if sealed:
        unseal(sealed[0], settings)
