"""The audit log (#317, ADR-0024): what changed, by which API key.

Every change made through the API to an agent, a project credential, a phone
number, an API key or a project setting writes an entry, in the transaction of
the change: a change rolled back leaves no entry, and a committed one always
has one. Routes record through an `AuditTrail`, which the API hands them
(`api/audit.py`); it refuses to let a successful request on those resources
commit without an entry.

What changed is a diff of two `State`s, field by field. A State holds what the
API reads (`public`, secrets masked) and what is compared (`raw`), so a
secret that changed shows as changed while its value never reaches the entry:
every value written comes from `public`. Credential values, API key material
(the raw key, its hash and its prefix), a number's auth token and its
call-init secret, and whatever the agent read masks are never recorded.

Retention is the operator's (`AUDIT_LOG_RETENTION_DAYS`, unset = for good),
never a project's, and call retention never touches the log.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from loguru import logger
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from turncall.domain.config_secrets import MASK, sanitize_config
from turncall.domain.enums import AuditAction, AuditActor, AuditResource
from turncall.storage.models import utc_now
from turncall.storage.repositories import audit_repo

BATCH = 1000
SWEEP_INTERVAL_SECONDS = 3600  # retention is in days; an hour late is in it

Clock = Callable[[], datetime]


class AuditEntryMissing(RuntimeError):
    """A request that changed an audited resource recorded no entry."""


@dataclass(frozen=True)
class State:
    """A resource as the audit log sees it at one moment.

    `public` is what may be recorded: the API's read of it, secrets masked.
    `raw` is what is compared; it may hold secrets, and is never written. A
    secret absent from `public` cannot be recorded at all.
    """

    public: dict[str, Any]
    raw: dict[str, Any]

    @classmethod
    def of(cls, public: dict[str, Any], raw: dict[str, Any] | None = None) -> State:
        # Plain JSON values, and copies: a row the change then updates in
        # place (the ORM's identity map) must not rewrite the "before".
        public = to_jsonable_python(public)
        return cls(public, public if raw is None else to_jsonable_python(raw))


_MISSING = object()


def diff(before: State | None, after: State | None) -> dict[str, dict[str, Any]]:
    """`{"dotted.path": {"before": ..., "after": ...}}` for every field that
    changed, values from `public` only. Dicts are compared key by key and
    lists of one length item by item; anything else is one field. A field
    absent on one side reads as null."""
    raw_before = before.raw if before else {}
    raw_after = after.raw if after else {}
    public_before = before.public if before else {}
    public_after = after.public if after else {}
    return {
        ".".join(str(part) for part in path): {
            "before": _at(public_before, path),
            "after": _at(public_after, path),
        }
        for path in _changed(raw_before, raw_after, ())
    }


def _changed(a: Any, b: Any, path: tuple[Any, ...]) -> Iterator[tuple[Any, ...]]:
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(a.keys() | b.keys(), key=str):
            yield from _changed(a.get(key), b.get(key), (*path, key))
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for index, (x, y) in enumerate(zip(a, b, strict=True)):
            yield from _changed(x, y, (*path, index))
    elif a != b:
        yield path


def _at(public: Any, path: tuple[Any, ...]) -> Any:
    """The public value at `path`. Where `public` masked a whole subtree,
    the mask; where it has nothing, null."""
    value = public
    for part in path:
        if value == MASK:
            return MASK
        if isinstance(value, dict):
            value = value.get(part, _MISSING)
        elif isinstance(value, list) and isinstance(part, int) and part < len(value):
            value = value[part]
        else:
            return None
        if value is _MISSING:
            return None
    return value


def _masked(value: Any) -> str | None:
    return MASK if value else None


# --- each resource as it is recorded --------------------------------------


def fields(**values: Any) -> State:
    """Some fields of a resource, none of them secret: what a side effect
    changed (a version a publish archived, a number it re-pointed), a key's
    revocation, a knowledge base link."""
    return State.of(values)


def agent_state(row: Any) -> State:
    """An agent version: its config as the agent read masks it."""
    raw = {
        "name": row.name,
        "environment": row.environment,
        "version": row.version,
        "state": row.state,
        "published_at": row.published_at,
        "config": row.config_blob or {},
    }
    return State.of({**raw, "config": sanitize_config(raw["config"])}, raw)


def knowledge_base_link(row: Any) -> dict[str, Any]:
    return {
        "knowledge_base_id": row.knowledge_base_id,
        "mode": row.mode,
        "priority": row.priority,
        "top_k": row.top_k,
        "similarity_threshold": row.similarity_threshold,
        "tool_description": row.tool_description,
    }


def phone_number_state(row: Any) -> State:
    """A binding as the phone number read returns it, but its call-init secret
    and its own Twilio auth token masked (the read never returns the token;
    it does return the secret, to the key that made the binding)."""
    from turncall.api.v1.schemas.phone_numbers import PhoneNumberResponse

    public = PhoneNumberResponse.from_row(row).model_dump(
        exclude={"id", "project_id", "created_at", "provider"}
    )
    raw = {
        **public,
        "server_url_secret": row.server_url_secret,
        "twilio_auth_token": getattr(row, "twilio_auth_token_ciphertext", None),
    }
    public["server_url_secret"] = _masked(row.server_url_secret)
    public["twilio_auth_token"] = _masked(raw["twilio_auth_token"])
    return State.of(public, raw)


def api_key_state(row: Any) -> State:
    """A key by what it can do, never by its material: no raw key, hash or
    prefix."""
    return State.of(
        {
            "name": row.name,
            "role": row.role,
            "environment": row.environment,
            "revoked_at": row.revoked_at,
        }
    )


def credential_state(row: Any | None) -> State | None:
    """A credential is its presence: the value is always masked. Its
    ciphertext is compared, and changes on every write (Fernet's IV), so a
    replace always reads as changed, never as what it was."""
    if row is None:
        return None
    return State.of({"value": MASK}, {"value": row.ciphertext})


def project_state(row: Any) -> State:
    """A project's settings as its read returns them, and whether it is
    deleted."""
    from turncall.api.v1.schemas.projects import ProjectResponse

    public = ProjectResponse.from_row(row).model_dump(
        include={"name", "limits", "redaction", "retention"}
    )
    return State.of({**public, "deleted_at": getattr(row, "deleted_at", None)})


# --- recording ------------------------------------------------------------


@dataclass
class AuditTrail:
    """One request's entries. `record` writes one in the request's session,
    so it commits or rolls back with the change. `on_record` is told every
    id so far (the API puts them on the response)."""

    session: AsyncSession
    actor: AuditActor
    api_key_id: UUID | None
    project_id: UUID | None
    request_id: str | None = None
    on_record: Callable[[list[UUID]], None] = lambda _ids: None
    ids: list[UUID] = field(default_factory=list)

    async def record(
        self,
        resource_type: AuditResource,
        resource_id: UUID | str,
        action: AuditAction,
        *,
        before: State | None = None,
        after: State | None = None,
        project_id: UUID | None = None,
    ) -> UUID:
        """Write an entry for one change, in the request's transaction. Call it
        before any commit the route makes, and after the change itself."""
        project = project_id or self.project_id
        if project is None:
            raise ValueError("an audit entry needs a project")
        entry_id = uuid4()
        await audit_repo.append(
            self.session,
            id=entry_id,
            project_id=project,
            api_key_id=self.api_key_id,
            actor=self.actor.value,
            resource_type=resource_type.value,
            resource_id=str(resource_id),
            action=action.value,
            changes=diff(before, after),
            request_id=self.request_id,
            created_at=utc_now(),
        )
        self.ids.append(entry_id)
        self.on_record(list(self.ids))
        return entry_id

    def require_entry(self, where: str) -> None:
        """A request that succeeded must have recorded its change."""
        if not self.ids:
            raise AuditEntryMissing(f"{where} changed an audited resource unrecorded")


# --- retention ------------------------------------------------------------


def system_clock() -> datetime:
    return datetime.now(UTC)


async def sweep(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    days: int | None,
    now: datetime,
) -> int:
    """Delete every project's entries older than `days`, in batches of
    `BATCH`, one transaction each. None keeps them all. The count deleted."""
    if days is None:
        return 0
    cutoff = now - timedelta(days=days)
    total = 0
    while True:
        async with session_factory() as session:
            deleted = await audit_repo.delete_older_than(session, cutoff, batch=BATCH)
            await session.commit()
        total += deleted
        if deleted < BATCH:
            return total


async def run_janitor(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    days: int | None,
    clock: Clock = system_clock,
    interval: float = SWEEP_INTERVAL_SECONDS,
) -> None:
    """Sweep every `interval` seconds until cancelled (the API's lifespan).
    With no retention set there is nothing to do, ever: it returns."""
    if days is None:
        return
    while True:
        try:
            await asyncio.sleep(interval)
            deleted = await sweep(session_factory, days=days, now=clock())
            if deleted:
                logger.info("audit_log_swept", deleted=deleted, days=days)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("audit_log_sweep_error")
