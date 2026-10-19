"""Project credentials — provider keys, Fernet-encrypted at rest (ADR-0020).

Nothing here returns a stored value to an API caller: the decrypting reader is
`load_documents`, which only the pipeline build calls.
"""

import json
from typing import Any
from uuid import UUID

from cryptography.fernet import InvalidToken
from loguru import logger
from sqlalchemy import Text, cast, delete, func, select, type_coerce
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.config.settings import Settings
from turncall.domain.config_secrets import map_secrets
from turncall.storage.models import (
    AgentRow,
    CallRow,
    EvalRunRow,
    EvalScenarioRow,
    PhoneNumberRow,
    ProjectCredentialRow,
    ProjectRow,
    SmsSessionRow,
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


# How many documents per column the startup check reads looking for a sealed
# secret: a match on `"tcs1:` can be a prompt that mentions the prefix.
SAMPLE_DOCUMENTS = 20


async def ensure_readable(session: AsyncSession, settings: Settings) -> None:
    """Startup check: fail closed when secrets exist and the key is unusable.

    Raises CredentialKeyError rather than letting the first call on a project
    with a credential discover it with a caller on the line. One stored value
    per column is opened: the key reads all of them or none. Every process
    that reads secrets runs it (the API and both workers), so each warns when
    the key is unset.

    Every column a secret is sealed in (#338) gives one sealed value, when it
    holds any. For the documents, a row whose text matches `"tcs1:` is walked
    to its secret paths, so a prompt that only mentions the prefix is no
    sample; at most SAMPLE_DOCUMENTS rows per column are looked at. Read past
    the column type, so the check sees what is stored whether or not the
    column unseals.
    """
    warn_if_no_key(settings)
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
    # The agent config in each: the whole blob, a call-init's inline config,
    # an inline eval target's agent. Each is walked as an agent config.
    for config in (
        AgentRow.config_blob,
        CallRow.metadata_json["dynamic_config"],
        SmsSessionRow.metadata_json["dynamic_config"],
        EvalRunRow.target["agent"],
        EvalScenarioRow.default_target["agent"],
    ):
        document = cast(type_coerce(config, JSONB), Text)
        sample = (
            select(document.label("doc"))
            .where(document.contains(f'"{PREFIX}', autoescape=True))
            .limit(SAMPLE_DOCUMENTS)
            .subquery()
        )
        found = await session.scalar(select(func.array_agg(sample.c.doc)))
        for stored in found or ():
            if _unseal_one(json.loads(stored), settings):
                break


def warn_if_no_key(settings: Settings) -> str | None:
    """With CREDENTIAL_ENCRYPTION_KEY unset a process still starts, but every
    write holding a secret is refused (#338), so say so before the first one
    is. Returns the warning, or None."""
    if settings.credentials.encryption_key:
        return None
    message = (
        "credential_encryption_key_unset | CREDENTIAL_ENCRYPTION_KEY is not set: "
        "creating a webhook subscription, giving a number a server_url, saving "
        "a project credential or an agent with a secret, and taking a call or "
        "a text whose call-init answers with one are refused (the API answers "
        "503 credential_key_unavailable) until it is"
    )
    logger.warning(message)
    return message


def _unseal_one(config: Any, settings: Settings) -> bool:
    """Open the first sealed secret in an agent config; whether it held one."""
    sealed: list[str] = []

    def collect(value: str) -> str:
        if value.startswith(PREFIX):
            sealed.append(value)
        return value

    map_secrets(config, collect)
    if sealed:
        unseal(sealed[0], settings)
    return bool(sealed)
