"""The `seal_secrets_at_rest` migration seals every stored secret, once (#338).

It carries its own frozen copy of the prefix and the secret paths, so these
tests load the revision file itself, in the importlib style of
test_weighted_campaign_downgrade.py, and swap its `op` for a fake.
"""

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

from turncall.domain.config_secrets import MASK, sanitize_config

pytestmark = pytest.mark.unit

MIGRATION = (
    Path(__file__).parents[2] / "alembic/versions/e6c2a8f4b0d9_seal_secrets_at_rest.py"
)

CONFIG: dict[str, Any] = {
    "name": "support",
    "system_prompt": "Be kind.",
    "llm": {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key": "sk-primary",
        "failover": [
            {
                "provider": "anthropic",
                "api_key": "sk-failover",
                "failover": [{"provider": "groq", "api_key": "sk-nested"}],
            },
            {"provider": "openai", "api_key": None},
        ],
    },
    "aws": {
        "region": "us-east-1",
        "access_key_id": "AKIAEXAMPLE",
        "secret_access_key": "aws-secret",
        "session_token": "aws-session",
    },
    "server_url": {"url": "https://example.com/events", "secret": "server-secret"},
    "tools": [
        {"name": "lookup", "webhook_secret": "tool-secret-0123456"},
        {"name": "unsigned", "webhook_secret": None},
        {"name": "plain"},
    ],
    "mcp_servers": [
        {
            "name": "crm",
            "headers": {"Authorization": "Bearer mcp-token"},
            "env": {"CRM_TOKEN": "mcp-env"},
        },
        {"name": "empty", "headers": {}, "env": None},
    ],
}

SECRETS = {
    "sk-primary",
    "sk-failover",
    "sk-nested",
    "AKIAEXAMPLE",
    "aws-secret",
    "aws-session",
    "server-secret",
    "tool-secret-0123456",
    "Bearer mcp-token",
    "mcp-env",
}


@pytest.fixture
def migration() -> Any:
    spec = importlib.util.spec_from_file_location("seal_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
    return key


@pytest.fixture
def no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)


def _strings(value: Any, path: str = "") -> dict[str, Any]:
    """Every leaf of a JSON document, by path."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out |= _strings(v, f"{path}.{k}")
        return out
    if isinstance(value, list):
        out = {}
        for i, v in enumerate(value):
            out |= _strings(v, f"{path}[{i}]")
        return out
    return {path: value}


def _found(value: Any, reach: Any) -> set[str]:
    """Every secret `reach` finds in `value`."""
    found: set[str] = set()

    def note(secret: str) -> str:
        found.add(secret)
        return secret

    reach(value, note)
    return found


# --- the frozen secret paths ---


def test_reaches_every_secret_in_a_config_and_nothing_else(migration):
    assert _found(CONFIG, migration.map_config) == SECRETS


def test_seals_every_path_the_masking_covers_today(migration):
    # sanitize_config is what #338 set out to seal (plus aws.access_key_id).
    # Pinned to this fixed config, so a later field added to the masking does
    # not make this frozen revision look wrong.
    sealed = _strings(migration.map_config(CONFIG, lambda v: "SEALED"))
    for path, value in _strings(sanitize_config(CONFIG)).items():
        if value == MASK:
            assert sealed[path] == "SEALED", path
    assert sealed[".aws.access_key_id"] == "SEALED"


def test_leaves_the_rest_of_the_config_and_its_input_alone(migration):
    original = copy.deepcopy(CONFIG)
    out = migration.map_config(CONFIG, lambda v: "SEALED")
    assert original == CONFIG
    for path, value in _strings(out).items():
        if value != "SEALED":
            assert _strings(CONFIG)[path] == value, path


def test_empty_and_missing_secrets_stay_as_they_are(migration):
    config = {"llm": {"api_key": ""}, "aws": {"session_token": None}, "tools": []}
    assert migration.map_config(config, lambda v: "SEALED") == config
    assert migration.map_text(None, lambda v: "SEALED") is None
    assert migration.map_text("", lambda v: "SEALED") == ""


def test_inline_targets_and_dynamic_configs_seal_only_their_agent(migration):
    target = {"type": "inline", "agent": CONFIG}
    assert _found(target, migration.map_inline_target) == SECRETS
    by_id = {"type": "agent", "agent_id": "a1"}
    assert migration.map_inline_target(by_id, lambda v: "SEALED") == by_id

    metadata = {"dynamic_config": CONFIG, "caller": "+15550100", "redaction": "done"}
    out = migration.map_dynamic_config(metadata, lambda v: "SEALED")
    assert _found(metadata, migration.map_dynamic_config) == SECRETS
    assert out["caller"] == "+15550100"
    assert out["redaction"] == "done"
    assert migration.map_dynamic_config({"caller": "x"}, str.upper) == {"caller": "x"}


# --- sealing ---


def test_seal_is_prefixed_and_unseals_to_the_original(migration, key):
    fernet = Fernet(key.encode())
    sealed = migration.sealer(fernet)("whsec")
    assert sealed.startswith("tcs1:")
    assert fernet.decrypt(sealed[len("tcs1:") :].encode()).decode() == "whsec"
    assert migration.unsealer(fernet)(sealed) == "whsec"


def test_seal_is_idempotent_and_unseal_passes_plaintext(migration, key):
    fernet = Fernet(key.encode())
    seal, unseal = migration.sealer(fernet), migration.unsealer(fernet)
    once = seal("whsec")
    assert seal(once) == once
    assert unseal("whsec") == "whsec"
    config = migration.map_config(CONFIG, seal)
    assert migration.map_config(config, seal) == config
    assert migration.map_config(config, unseal) == CONFIG


def test_unseal_under_another_key_says_so(migration):
    sealed = migration.sealer(Fernet(Fernet.generate_key()))("whsec")
    with pytest.raises(RuntimeError, match="rotated"):
        migration.unsealer(Fernet(Fernet.generate_key()))(sealed)


def test_a_value_that_only_looks_sealed_is_sealed(migration, key):
    """As the app's `seal`: prefixed counts as sealed only if the key opens it,
    or a plaintext secret starting `tcs1:` would be left for the app to fail
    reading."""
    fernet = Fernet(key.encode())
    seal, unseal = migration.sealer(fernet), migration.unsealer(fernet)
    for lookalike in ("tcs1:my-own-secret", "tcs1:", seal("x")[:-4]):
        assert not migration.sealed_under(fernet, lookalike)
        sealed = seal(lookalike)
        assert sealed != lookalike
        assert unseal(sealed) == lookalike
    assert migration.sealed_under(fernet, seal("whsec"))
    assert not migration.sealed_under(None, seal("whsec"))


# --- upgrade / downgrade against a fake op ---


class _Op:
    """Rows per table; logs the ALTERs and updates, in the order issued.

    The SQL candidate filter is not applied: every row of a table is offered,
    and the migration's own walk must skip what holds no secret.
    """

    def __init__(self, rows: dict[str, list[tuple[Any, Any]]]) -> None:
        self.rows = rows
        self.log: list[tuple[str, str]] = []
        self.updates: list[tuple[str, Any, Any]] = []
        self.batches: list[int] = []

    def _execute(self, statement: Any, params: Any = None) -> Any:
        if isinstance(statement, sa.Update):
            assert isinstance(params, list), "updates go out batched"
            self.batches.append(len(params))
            for p in params:
                self.updates.append((statement.table.name, p["row_id"], p["sealed"]))
                self.log.append(("update", statement.table.name))
            return None
        # A page: rows after the last id seen, in id order, up to the limit.
        compiled = statement.compile().params
        after = compiled.get("id_1")
        rows = sorted(self.rows.get(statement.get_final_froms()[0].name, []))
        page = [r for r in rows if after is None or r[0] > after]
        page = page[: statement._limit]
        return SimpleNamespace(all=lambda: page)

    def get_bind(self) -> Any:
        return SimpleNamespace(execute=self._execute)

    def alter_column(self, table: str, *args: Any, **kwargs: Any) -> None:
        self.log.append(("alter", table))

    @property
    def alters(self) -> list[str]:
        return [table for kind, table in self.log if kind == "alter"]


WIDENED = ["webhook_subscriptions", "phone_numbers"]


def _with(migration: Any, op: _Op) -> Any:
    migration.op = op
    return migration


def test_upgrade_with_nothing_to_seal_needs_no_key(migration, no_key):
    op = _Op({"agents": [("a1", {"name": "no secrets"})]})
    _with(migration, op).upgrade()
    assert op.alters == WIDENED
    assert op.updates == []


def test_upgrade_refuses_without_a_key_and_changes_nothing(migration, no_key):
    op = _Op(
        {
            "webhook_subscriptions": [("w1", "whsec")],
            "agents": [("a1", CONFIG), ("a2", {"name": "no secrets"})],
        }
    )
    with pytest.raises(RuntimeError) as refused:
        _with(migration, op).upgrade()
    message = str(refused.value)
    assert "2 row(s)" in message
    assert "webhook_subscriptions.secret: 1" in message
    assert "agents.config_blob: 1" in message
    assert "CREDENTIAL_ENCRYPTION_KEY is not set" in message
    assert op.log == []


def test_without_a_key_a_prefixed_value_is_not_taken_as_sealed(migration, no_key):
    """No key opens it, so it cannot be told from a secret that starts `tcs1:`."""
    op = _Op({"webhook_subscriptions": [("w1", "tcs1:already")]})
    with pytest.raises(RuntimeError, match=r"webhook_subscriptions\.secret: 1"):
        _with(migration, op).upgrade()
    assert op.log == []


def test_upgrade_seals_each_row_holding_plaintext(migration, key):
    fernet = Fernet(key.encode())
    sealed = migration.sealer(fernet)("sealed-before")
    op = _Op(
        {
            "webhook_subscriptions": [
                ("w1", "whsec"),
                ("w2", sealed),
                ("w3", "tcs1:lookalike"),
            ],
            "agents": [("a1", CONFIG), ("a2", {"name": "no secrets"})],
        }
    )
    _with(migration, op).upgrade()
    unseal = migration.unsealer(fernet)
    assert [(t, i) for t, i, _ in op.updates] == [
        ("agents", "a1"),
        ("webhook_subscriptions", "w1"),
        ("webhook_subscriptions", "w3"),
    ]
    (_, _, config), (_, _, secret), (_, _, lookalike) = op.updates
    assert unseal(secret) == "whsec"
    assert unseal(lookalike) == "tcs1:lookalike"
    assert migration.map_config(config, unseal) == CONFIG
    assert not migration.holds(
        config, migration.map_config, lambda v: not migration.sealed_under(fernet, v)
    )


def test_upgrade_takes_the_alter_lock_after_the_large_tables(migration, key):
    """The ALTER's exclusive lock lasts to commit: the large tables are sealed
    before it is taken, and only the two small ones after."""
    op = _Op(
        {
            "webhook_subscriptions": [("w1", "whsec")],
            "phone_numbers": [("p1", "srv")],
            "agents": [("a1", CONFIG)],
            "calls": [("c1", {"dynamic_config": CONFIG})],
            "sms_sessions": [("s1", {"dynamic_config": CONFIG})],
            "eval_runs": [("r1", {"type": "inline", "agent": CONFIG})],
            "eval_scenarios": [("e1", {"type": "inline", "agent": CONFIG})],
        }
    )
    _with(migration, op).upgrade()
    first_alter = op.log.index(("alter", "webhook_subscriptions"))
    before = {table for _, table in op.log[:first_alter]}
    after = {table for kind, table in op.log[first_alter:] if kind == "update"}
    assert before == {"agents", "calls", "sms_sessions", "eval_runs", "eval_scenarios"}
    assert after == set(WIDENED)


def test_upgrade_pages_and_batches_every_row_once(migration, key, monkeypatch):
    monkeypatch.setattr(migration, "PAGE", 2)
    op = _Op({"webhook_subscriptions": [(f"w{i}", f"whsec{i}") for i in range(5)]})
    _with(migration, op).upgrade()
    assert [i for _, i, _ in op.updates] == ["w0", "w1", "w2", "w3", "w4"]
    assert op.batches == [2, 2, 1]


def test_upgrade_refuses_a_malformed_key(migration, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "not-a-key")
    op = _Op({"webhook_subscriptions": [("w1", "whsec")]})
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        _with(migration, op).upgrade()
    assert op.log == []


def test_downgrade_refuses_sealed_rows_without_a_key(migration, no_key):
    op = _Op({"phone_numbers": [("p1", "tcs1:sealed")]})
    with pytest.raises(RuntimeError, match=r"phone_numbers\.server_url_secret: 1"):
        _with(migration, op).downgrade()
    assert op.log == []


def test_downgrade_unseals_then_narrows(migration, key):
    seal = migration.sealer(Fernet(key.encode()))
    sealed_config = migration.map_config(CONFIG, seal)
    op = _Op(
        {
            "phone_numbers": [("p1", seal("srv"))],
            "eval_runs": [("r1", {"type": "inline", "agent": sealed_config})],
        }
    )
    _with(migration, op).downgrade()
    assert op.updates == [
        ("eval_runs", "r1", {"type": "inline", "agent": CONFIG}),
        ("phone_numbers", "p1", "srv"),
    ]
    # Narrowed last: no row is rewritten under the ALTER's lock.
    assert op.log[-2:] == [("alter", t) for t in WIDENED]
    assert op.alters == WIDENED


def test_downgrade_with_nothing_sealed_needs_no_key(migration, no_key):
    op = _Op({"webhook_subscriptions": [("w1", "plaintext")]})
    _with(migration, op).downgrade()
    assert op.alters == WIDENED
    assert op.updates == []
