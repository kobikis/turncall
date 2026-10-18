"""secrets at rest: seal every stored secret under CREDENTIAL_ENCRYPTION_KEY (#338)

Revision ID: e6c2a8f4b0d9
Revises: d4b8e2f6a0c3
Create Date: 2026-10-18
"""

import os
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "e6c2a8f4b0d9"
down_revision: str | None = "d4b8e2f6a0c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies, not imports: what this revision seals is what the code
# sealed when it was written. A later change to the app's prefix or secret
# paths is a later revision's to migrate, never a silent change to this one.
PREFIX = "tcs1:"
# The variable the app reads its key from (ADR-0020): one key for both.
KEY_ENV = "CREDENTIAL_ENCRYPTION_KEY"
PAGE = 500

Convert = Callable[[str], str]


def map_text(value: Any, fn: Convert) -> Any:
    """A secret column, or one secret in a config: a non-empty string."""
    return fn(value) if isinstance(value, str) and value else value


def _llm(llm: dict[str, Any], fn: Convert) -> dict[str, Any]:
    out = {**llm}
    if "api_key" in out:
        out["api_key"] = map_text(out["api_key"], fn)
    if isinstance(out.get("failover"), list):
        out["failover"] = [
            _llm(e, fn) if isinstance(e, dict) else e for e in out["failover"]
        ]
    return out


def _values(mapping: Any, fn: Convert) -> Any:
    if not isinstance(mapping, dict):
        return mapping
    return {k: map_text(v, fn) for k, v in mapping.items()}


def map_config(config: Any, fn: Convert) -> Any:
    """Apply `fn` to every secret an agent config carries, as of #338:
    llm.api_key (and each failover's, recursively), aws.access_key_id /
    secret_access_key / session_token, server_url.secret,
    tools[].webhook_secret, and every mcp_servers[].headers / .env value."""
    if not isinstance(config, dict):
        return config
    out = {**config}
    if isinstance(out.get("llm"), dict):
        out["llm"] = _llm(out["llm"], fn)
    if isinstance(out.get("aws"), dict):
        aws = out["aws"]
        out["aws"] = {
            **aws,
            **{
                k: map_text(aws[k], fn)
                for k in ("access_key_id", "secret_access_key", "session_token")
                if k in aws
            },
        }
    if isinstance(out.get("server_url"), dict) and "secret" in out["server_url"]:
        server_url = out["server_url"]
        out["server_url"] = {**server_url, "secret": map_text(server_url["secret"], fn)}
    if isinstance(out.get("tools"), list):
        out["tools"] = [
            {**t, "webhook_secret": map_text(t["webhook_secret"], fn)}
            if isinstance(t, dict) and "webhook_secret" in t
            else t
            for t in out["tools"]
        ]
    if isinstance(out.get("mcp_servers"), list):
        out["mcp_servers"] = [
            {
                **s,
                **{k: _values(s[k], fn) for k in ("headers", "env") if k in s},
            }
            if isinstance(s, dict)
            else s
            for s in out["mcp_servers"]
        ]
    return out


def map_inline_target(target: Any, fn: Convert) -> Any:
    """An eval target `{"type": "inline", "agent": {...}}`."""
    if not isinstance(target, dict) or not isinstance(target.get("agent"), dict):
        return target
    return {**target, "agent": map_config(target["agent"], fn)}


def map_dynamic_config(metadata: Any, fn: Convert) -> Any:
    """The inline agent call-init answered with, in `metadata_json`."""
    if not isinstance(metadata, dict) or not isinstance(
        metadata.get("dynamic_config"), dict
    ):
        return metadata
    return {**metadata, "dynamic_config": map_config(metadata["dynamic_config"], fn)}


# (table, column, column type, which rows can hold one, how to reach it).
# Never audit_log: it holds masked values only, and is append-only.
COLUMNS: tuple[tuple[str, str, Any, str, Callable[[Any, Convert], Any]], ...] = (
    ("webhook_subscriptions", "secret", sa.Text(), "secret IS NOT NULL", map_text),
    (
        "phone_numbers",
        "server_url_secret",
        sa.Text(),
        "server_url_secret IS NOT NULL",
        map_text,
    ),
    ("agents", "config_blob", JSONB(), "config_blob IS NOT NULL", map_config),
    (
        "calls",
        "metadata_json",
        JSONB(),
        "metadata_json -> 'dynamic_config' IS NOT NULL",
        map_dynamic_config,
    ),
    (
        "sms_sessions",
        "metadata_json",
        JSONB(),
        "metadata_json -> 'dynamic_config' IS NOT NULL",
        map_dynamic_config,
    ),
    (
        "eval_runs",
        "target",
        JSONB(),
        "target -> 'agent' IS NOT NULL",
        map_inline_target,
    ),
    (
        "eval_scenarios",
        "default_target",
        JSONB(),
        "default_target -> 'agent' IS NOT NULL",
        map_inline_target,
    ),
)

# A Fernet token for a 43-character secret is 140 characters before the prefix.
WIDENED = (
    ("webhook_subscriptions", "secret", False),
    ("phone_numbers", "server_url_secret", True),
)


def is_sealed(value: str) -> bool:
    return value.startswith(PREFIX)


def _fernet() -> Fernet | None:
    key = os.environ.get(KEY_ENV, "")
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise RuntimeError(
            f"{KEY_ENV} is not a valid Fernet key (32 url-safe base64-encoded bytes)."
        ) from exc


def sealer(fernet: Fernet) -> Convert:
    def seal(value: str) -> str:
        if is_sealed(value):
            return value
        return PREFIX + fernet.encrypt(value.encode()).decode()

    return seal


def unsealer(fernet: Fernet) -> Convert:
    def unseal(value: str) -> str:
        if not is_sealed(value):
            return value
        try:
            return fernet.decrypt(value[len(PREFIX) :].encode()).decode()
        except InvalidToken as exc:
            raise RuntimeError(
                f"A sealed secret cannot be decrypted with the configured {KEY_ENV}"
                " — was the key rotated?"
            ) from exc

    return unseal


def _rows(bind: Any, table: str, column: str, type_: Any, where: str) -> Iterator[Any]:
    """Every row that can hold a secret, a page at a time, by id."""
    t = sa.table(table, sa.column("id", UUID(as_uuid=True)), sa.column(column, type_))
    after = None
    while True:
        query = sa.select(t.c.id, t.c[column]).where(sa.text(where))
        if after is not None:
            query = query.where(t.c.id > after)
        page = bind.execute(query.order_by(t.c.id).limit(PAGE)).all()
        yield from page
        if len(page) < PAGE:
            return
        after = page[-1][0]


def holds(value: Any, reach: Callable[[Any, Convert], Any], wanted: Any) -> bool:
    """Whether any secret `reach` finds in `value` is one `wanted` picks out."""
    found: list[str] = []

    def note(secret: str) -> str:
        if wanted(secret):
            found.append(secret)
        return secret

    reach(value, note)
    return bool(found)


def _pending(bind: Any, wanted: Callable[[str], bool]) -> dict[str, int]:
    """How many rows per column hold a secret `wanted` picks out."""
    counts: dict[str, int] = {}
    for table, column, type_, where, reach in COLUMNS:
        count = sum(
            holds(value, reach, wanted)
            for _id, value in _rows(bind, table, column, type_, where)
        )
        if count:
            counts[f"{table}.{column}"] = count
    return counts


def _rewrite(bind: Any, fn: Convert) -> None:
    for table, column, type_, where, reach in COLUMNS:
        t = sa.table(
            table, sa.column("id", UUID(as_uuid=True)), sa.column(column, type_)
        )
        # Each page is read whole before its updates run, and an updated row
        # still sorts where it did, so paging by id sees every row once.
        for row_id, value in _rows(bind, table, column, type_, where):
            new = reach(value, fn)
            if new != value:
                bind.execute(sa.update(t).where(t.c.id == row_id).values({column: new}))


def _summary(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    where = ", ".join(f"{name}: {n}" for name, n in counts.items())
    return f"{total} row(s) ({where})"


def upgrade() -> None:
    bind = op.get_bind()
    # Checked before anything changes, so a refusal leaves the schema as is.
    plaintext = _pending(bind, lambda v: not is_sealed(v))
    fernet = _fernet()
    if plaintext and fernet is None:
        raise RuntimeError(
            f"{_summary(plaintext)} hold secrets in plaintext, and {KEY_ENV} is "
            "not set, so they cannot be sealed. Set it — once, and never rotate "
            'it — then upgrade again. Generate one with `python -c "from '
            "cryptography.fernet import Fernet; print(Fernet.generate_key()"
            '.decode())"`.'
        )
    for table, column, nullable in WIDENED:
        op.alter_column(
            table,
            column,
            existing_type=sa.String(128),
            type_=sa.Text(),
            existing_nullable=nullable,
        )
    if plaintext and fernet is not None:
        _rewrite(bind, sealer(fernet))


def downgrade() -> None:
    bind = op.get_bind()
    sealed = _pending(bind, is_sealed)
    fernet = _fernet()
    if sealed and fernet is None:
        raise RuntimeError(
            f"{_summary(sealed)} hold sealed secrets, which the previous revision "
            f"reads as plaintext, and {KEY_ENV} is not set, so they cannot be "
            "unsealed. Set it to the key they were sealed with, then downgrade "
            "again."
        )
    if sealed and fernet is not None:
        _rewrite(bind, unsealer(fernet))
    for table, column, nullable in WIDENED:
        op.alter_column(
            table,
            column,
            existing_type=sa.Text(),
            type_=sa.String(128),
            existing_nullable=nullable,
        )
