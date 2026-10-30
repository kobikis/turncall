"""The audit log (#317, ADR-0024): what changed, by which API key.

Every change made through the API to an agent, a skill, a project credential,
a phone number, an API key or a project setting writes an entry, in the transaction of
the change: a change rolled back leaves no entry, and a committed one always
has one. Routes record through an `AuditTrail`, which the API hands them
(`api/audit.py`) bound to the request's session. The trail holds its entries
until the session commits, then writes them in one INSERT just before the
commit; a commit of a session whose trail recorded nothing is refused
(`before_commit`), whoever commits it, so a change on those resources is never
saved unrecorded.

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
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from loguru import logger
from pydantic_core import to_jsonable_python
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from turncall.domain.config_secrets import MASK, sanitize_config, sanitize_tools
from turncall.domain.enums import AuditAction, AuditActor, AuditResource
from turncall.storage.models import utc_now
from turncall.storage.repositories import audit_repo

BATCH = 1000
SWEEP_INTERVAL_SECONDS = 3600  # retention is in days; an hour late is in it

# Where a session holds its request's trail (`AuditTrail.bind`).
TRAIL = "turncall.audit_trail"


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


def pointer(path: tuple[Any, ...]) -> str:
    """The RFC 6901 JSON Pointer to `path`: `/config/llm/api_key`. A key
    holding `~` or `/` is escaped (`~0`, `~1`), so a key with a `.` or a `/`
    in it never reads as a nested path, and two paths never collide."""
    return "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in path
    )


def diff(before: State | None, after: State | None) -> dict[str, dict[str, Any]]:
    """`{"/json/pointer": {"before": ..., "after": ...}}` for every field that
    changed, values from `public` only. Dicts are compared key by key and
    lists of one length item by item; anything else is one field. A field
    absent on one side reads as null."""
    raw_before = before.raw if before else {}
    raw_after = after.raw if after else {}
    public_before = before.public if before else {}
    public_after = after.public if after else {}
    return {
        pointer(path): {
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


def skill_state(row: Any) -> State:
    """A skill version (#423): its tools as the skill read masks them."""
    raw = {
        "name": row.name,
        "version": row.version,
        "state": row.state,
        "published_at": row.published_at,
        "description": row.description,
        "instructions": row.instructions,
        "tools": row.tools or [],
        "knowledge_bases": row.knowledge_bases or [],
    }
    return State.of({**raw, "tools": sanitize_tools(raw["tools"])}, raw)


def knowledge_base_link(row: Any) -> State:
    """An agent's link to a knowledge base, as one field of the agent."""
    return fields(
        knowledge_base={
            "knowledge_base_id": row.knowledge_base_id,
            "mode": row.mode,
            "priority": row.priority,
            "top_k": row.top_k,
            "similarity_threshold": row.similarity_threshold,
            "tool_description": row.tool_description,
        }
    )


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
        include={"name", "limits", "redaction", "retention", "skill_library"}
    )
    return State.of({**public, "deleted_at": getattr(row, "deleted_at", None)})


# --- recording ------------------------------------------------------------


@dataclass
class Change:
    """One resource being changed (`AuditTrail.change`): its state before,
    taken when the change began, and the state it ends in."""

    state: Callable[[Any], State | None]
    before: State | None
    after: State | None = None
    ended: bool = False

    def to(self, row: Any) -> None:
        """The resource after the change: `row`, read through the same state."""
        self.after = self.state(row)
        self.ended = True

    def gone(self) -> None:
        """The change removed the resource."""
        self.after = None
        self.ended = True


@dataclass
class AuditTrail:
    """One request's entries. `record` adds one; they are written in the
    request's session, in one INSERT, when it commits (`bind`), so they
    commit or roll back with the change.

    `request_id` is the id of the request's first entry, the one for its
    primary resource, and every entry the request writes carries it: the API
    names it on the response (`on_first`), and reading by it returns the
    whole request (a publish's archived versions and re-pointed numbers)."""

    actor: AuditActor
    api_key_id: UUID | None
    project_id: UUID | None
    where: str = "a request"
    on_first: Callable[[UUID], None] = lambda _id: None
    request_id: UUID | None = None
    recorded: int = 0
    pending: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def bind(cls, session: AsyncSession | Session, **values: Any) -> AuditTrail:
        """A trail whose entries `session` writes when it commits, and whose
        commit is refused if it recorded nothing."""
        trail = cls(**values)
        session.info[TRAIL] = trail
        return trail

    def record(
        self,
        resource_type: AuditResource,
        resource_id: UUID | str,
        action: AuditAction,
        *,
        before: State | None = None,
        after: State | None = None,
        project_id: UUID | None = None,
    ) -> UUID:
        """Add an entry for one change, written with the request's commit.
        The first entry is the request's primary one. A change of a resource
        that existed goes through `change`, which takes its "before"."""
        project = project_id or self.project_id
        if project is None:
            raise ValueError("an audit entry needs a project")
        entry_id = uuid4()
        if self.request_id is None:
            self.request_id = entry_id
            self.on_first(entry_id)
        self.pending.append(
            {
                "id": entry_id,
                "project_id": project,
                "api_key_id": self.api_key_id,
                "actor": self.actor.value,
                "resource_type": resource_type.value,
                "resource_id": str(resource_id),
                "action": action.value,
                "changes": diff(before, after),
                "request_id": self.request_id,
                "created_at": utc_now(),
            }
        )
        self.recorded += 1
        return entry_id

    @contextmanager
    def change(
        self,
        resource_type: AuditResource,
        resource_id: UUID | str,
        action: AuditAction,
        state: Callable[[Any], State | None],
        current: Any | None,
        *,
        project_id: UUID | None = None,
    ) -> Iterator[Change]:
        """Record a change of `current` (the row as read, None if there is
        none) made in the block: its state is taken on entry, before the
        block can write, so the repos updating that same ORM object in place
        cannot rewrite the "before". The block ends by saying what the
        resource became, `change.to(row)` or `change.gone()`; it is recorded
        when the block exits without an error.

            with audit.change(AGENT, agent_id, UPDATED, agent_state, existing) as c:
                row = await agent_repo.update_agent_config(...)
                c.to(row)
        """
        change = Change(state, state(current) if current is not None else None)
        yield change
        if not change.ended:
            raise RuntimeError(
                "an audited change must end with change.to(row) or change.gone()"
            )
        self.record(
            resource_type,
            resource_id,
            action,
            before=change.before,
            after=change.after,
            project_id=project_id,
        )

    def committing(self, session: Session) -> None:
        """The session is about to commit: write what is pending, or refuse
        the commit of a request that recorded nothing."""
        if not self.recorded:
            raise AuditEntryMissing(
                f"{self.where} changed an audited resource unrecorded"
            )
        if self.pending:
            audit_repo.insert_entries(session, self.pending)
            self.pending = []


@event.listens_for(Session, "before_commit")
def _before_commit(session: Session) -> None:
    trail = session.info.get(TRAIL)
    if trail is not None:
        trail.committing(session)


# --- retention ------------------------------------------------------------


async def sweep(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    days: int,
    now: datetime,
) -> int:
    """Delete every project's entries older than `days`, in batches of
    `BATCH`, one transaction each. The count deleted.

    Every API replica sweeps: a batch skips the rows another replica's batch
    has locked, so they never wait on or deadlock with each other, and the
    sweep goes on until a batch finds nothing left to take, not until one
    comes up short (it is short when another replica took rows)."""
    cutoff = now - timedelta(days=days)
    total = 0
    while True:
        async with session_factory() as session:
            deleted = await audit_repo.delete_older_than(session, cutoff, batch=BATCH)
            await session.commit()
        if not deleted:
            return total
        total += deleted


async def run_janitor(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    days: int | None,
    clock: Callable[[], datetime] = utc_now,
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
