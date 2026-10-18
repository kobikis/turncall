"""Secrets sealed at rest under CREDENTIAL_ENCRYPTION_KEY (ADR-0020, #338).

Project credentials and a number's Twilio token were the only secrets stored
encrypted; webhook signing secrets, a number's server_url secret and every
secret an agent config carries were plaintext in their rows. This seals them
with the same Fernet key, one value at a time, so a dump of the database alone
reads none of them.

A sealed value is `PREFIX` + a Fernet token. The prefix, not Fernet's own
`gAAAAA` lead, tells sealed from plaintext: a user-chosen secret can start with
anything, and a fixed versioned marker makes the migration re-runnable, lets
the startup check find a sample with one LIKE, and lets a test assert "no
plaintext" with one query. The migration freezes its own copy of the prefix and
the paths, so what is here may change without rewriting an old revision.

The column types below seal on write and unseal on read, at the ORM boundary:
about twenty readers take a config dict straight off a row (the pipeline, the
post-call work, evals, the audit diff), and every one of them keeps getting
plaintext without being touched. A raw-SQL read or a mocked session sees the
stored value as it is.

No key, no write: a value to seal with CREDENTIAL_ENCRYPTION_KEY unset raises
CredentialKeyError, never a silent fall back to plaintext. A value with no
secret in it needs no key. Raised from a bind processor, the error reaches the
caller wrapped in SQLAlchemy's StatementError, with this one as `.orig`.
"""

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Dialect, Text, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

from turncall.config.settings import Settings, get_settings
from turncall.domain.config_secrets import (
    map_dynamic_config,
    map_inline_target,
    map_secrets,
)

PREFIX = "tcs1:"


class CredentialKeyError(RuntimeError):
    """CREDENTIAL_ENCRYPTION_KEY is unset, malformed, or not the one used."""


def fernet(settings: Settings | None = None) -> Fernet:
    """The cipher for CREDENTIAL_ENCRYPTION_KEY; CredentialKeyError without one."""
    key = (settings or get_settings()).credentials.encryption_key
    if not key:
        raise CredentialKeyError(
            "CREDENTIAL_ENCRYPTION_KEY is not set, so project credentials and "
            "the secrets sealed at rest cannot be stored or read. Generate one "
            'with `python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` and set it once — '
            "rotating it orphans every stored secret."
        )
    return _cipher(key)


@lru_cache(maxsize=4)
def _cipher(key: str) -> Fernet:
    """One Fernet per key, not one per value sealed or opened: a config holds
    a dozen secrets, and a page of calls a config each. A malformed key raises
    each time it is asked for; lru_cache keeps no exception."""
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise CredentialKeyError(
            "CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key "
            "(32 url-safe base64-encoded bytes)."
        ) from exc


def is_sealed(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def seal(value: str, settings: Settings | None = None) -> str:
    """`value` sealed under the key; None and "" come back as they are.

    Idempotent: a value already sealed under this key is returned unchanged.
    One that only looks sealed — a user's secret that happens to start with
    the prefix — is sealed like any other, or reading it back would fail.
    """
    if not value:
        return value
    cipher = fernet(settings)
    if is_sealed(value):
        try:
            cipher.decrypt(value[len(PREFIX) :].encode())
        except InvalidToken:
            pass
        else:
            return value
    return PREFIX + cipher.encrypt(value.encode()).decode()


def unseal(value: str, settings: Settings | None = None) -> str:
    """The plaintext of a sealed `value`; anything unsealed passes through.

    The passthrough is for a row written before the migration ran (a rolling
    deploy) and for test fakes. A sealed value the key cannot open raises
    CredentialKeyError rather than handing the caller ciphertext as a secret.
    """
    if not is_sealed(value):
        return value
    try:
        return fernet(settings).decrypt(value[len(PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise CredentialKeyError(
            "A secret sealed at rest cannot be decrypted with the configured "
            "CREDENTIAL_ENCRYPTION_KEY — was the key rotated?"
        ) from exc


class SealedText(TypeDecorator[str]):
    """A secret column: webhook_subscriptions.secret, phone_numbers.server_url_secret.

    Text, not String(128): a 43-character token_urlsafe(32) secret is about
    145 characters sealed.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        return None if value is None else seal(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        return None if value is None else unseal(value)


class _PlainPaths(TypeDecorator.Comparator[Any], JSONB.Comparator[Any]):
    """A path into a sealed document is a path into plain JSONB.

    Left as the decorator, `col["llm"]` would run the whole-config walker over
    the `llm` block on the way out, find no `llm` key in it, and hand back its
    api_key still sealed while looking unsealed. It also renders as it did on
    JSONB (PG14's `col['k']`, which SQLAlchemy only picks for a JSONB-typed
    left side), so no query into the column changes.

    The hazard: a path reads what is stored, ciphertext included, as raw SQL
    does. `col["llm"]["api_key"]` selected, compared or written is the sealed
    value, never the secret, and a value written by path is not sealed. Only
    a whole-column read unseals, so read the whole column for a secret. No
    code reads one by path; test_sealing pins the path reads there are, so a
    new one is looked at before it lands.
    """

    def __getitem__(self, index: Any) -> Any:
        return type_coerce(self.expr, JSONB())[index]


class _SealedJSONB(TypeDecorator[Any]):
    """JSONB whose secret paths, named by `_walk`, are sealed in the row.

    Only the secret strings change — the document keeps its shape, so a path
    expression (`col["analysis"]["takeaway_ids"]`, `.astext`) compiles and
    matches as it did on plain JSONB. A non-dict value passes through.
    """

    impl = JSONB
    cache_ok = True
    comparator_factory = _PlainPaths

    def _walk(self, value: Any, fn: Callable[[str], str]) -> Any:
        raise NotImplementedError

    def coerce_compared_value(self, op: Any, value: Any) -> Any:
        """An operand binds as it did against JSONB, unless JSONB would bind it
        as JSONB: then as this type, so a document merged in is sealed too.

        A TypeDecorator binds every operand as itself, so the key in the SMS
        claim's `metadata_json - 'claimed_until'` went out as a JSON string,
        `jsonb - jsonb`, which Postgres has no operator for.
        """
        coerced = self.impl_instance.coerce_compared_value(op, value)
        return self if coerced is self.impl_instance else coerced

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        return self._walk(value, seal)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        return self._walk(value, unseal)


class SealedAgentConfig(_SealedJSONB):
    """agents.config_blob: every secret `map_secrets` names."""

    cache_ok = True  # read off each class's own __dict__, never inherited

    def _walk(self, value: Any, fn: Callable[[str], str]) -> Any:
        return map_secrets(value, fn)


class SealedTarget(_SealedJSONB):
    """eval_runs.target, eval_scenarios.default_target: an inline target's agent."""

    cache_ok = True  # read off each class's own __dict__, never inherited

    def _walk(self, value: Any, fn: Callable[[str], str]) -> Any:
        return map_inline_target(value, fn)


class SealedMetadata(_SealedJSONB):
    """calls.metadata_json, sms_sessions.metadata_json: the `dynamic_config` only."""

    cache_ok = True  # read off each class's own __dict__, never inherited

    def _walk(self, value: Any, fn: Callable[[str], str]) -> Any:
        return map_dynamic_config(value, fn)
