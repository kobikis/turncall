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


# --- upgrade / downgrade against a fake op ---


class _Op:
    """Rows per table; records DDL and the updates the migration issues."""

    def __init__(self, rows: dict[str, list[tuple[Any, Any]]]) -> None:
        self.rows = rows
        self.calls: list[str] = []
        self.updates: list[tuple[str, Any, Any]] = []

    def _execute(self, statement: Any) -> Any:
        if isinstance(statement, sa.Update):
            params = statement.compile().params
            row_id = params.pop("id_1")
            (value,) = params.values()
            self.updates.append((statement.table.name, row_id, value))
            return None
        # A page: rows after the last id seen, in id order, up to the limit.
        params = statement.compile().params
        after = params.get("id_1")
        rows = sorted(self.rows.get(statement.get_final_froms()[0].name, []))
        page = [r for r in rows if after is None or r[0] > after]
        page = page[: statement._limit]
        return SimpleNamespace(all=lambda: page)

    def get_bind(self) -> Any:
        return SimpleNamespace(execute=self._execute)

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: self.calls.append(name)


def _with(migration: Any, op: _Op) -> Any:
    migration.op = op
    return migration


def test_upgrade_with_nothing_to_seal_needs_no_key(migration, no_key):
    op = _Op({})
    _with(migration, op).upgrade()
    assert op.calls == ["alter_column", "alter_column"]
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
    assert op.calls == []
    assert op.updates == []


def test_upgrade_with_only_sealed_secrets_needs_no_key(migration, no_key):
    op = _Op({"webhook_subscriptions": [("w1", "tcs1:already")]})
    _with(migration, op).upgrade()
    assert op.updates == []


def test_upgrade_seals_each_row_holding_plaintext(migration, key):
    op = _Op(
        {
            "webhook_subscriptions": [("w1", "whsec"), ("w2", "tcs1:already")],
            "agents": [("a1", CONFIG), ("a2", {"name": "no secrets"})],
        }
    )
    _with(migration, op).upgrade()
    unseal = migration.unsealer(Fernet(key.encode()))
    assert op.calls == ["alter_column", "alter_column"]
    assert [(t, i) for t, i, _ in op.updates] == [
        ("webhook_subscriptions", "w1"),
        ("agents", "a1"),
    ]
    (_, _, secret), (_, _, config) = op.updates
    assert unseal(secret) == "whsec"
    assert migration.map_config(config, unseal) == CONFIG
    assert not migration.holds(
        config, migration.map_config, lambda v: not migration.is_sealed(v)
    )


def test_upgrade_pages_through_every_row_once(migration, key, monkeypatch):
    monkeypatch.setattr(migration, "PAGE", 2)
    op = _Op({"webhook_subscriptions": [(f"w{i}", f"whsec{i}") for i in range(5)]})
    _with(migration, op).upgrade()
    assert [i for _, i, _ in op.updates] == ["w0", "w1", "w2", "w3", "w4"]


def test_upgrade_refuses_a_malformed_key(migration, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "not-a-key")
    op = _Op({"webhook_subscriptions": [("w1", "whsec")]})
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        _with(migration, op).upgrade()
    assert op.calls == []


def test_downgrade_refuses_sealed_rows_without_a_key(migration, no_key):
    op = _Op({"phone_numbers": [("p1", "tcs1:sealed")]})
    with pytest.raises(RuntimeError, match=r"phone_numbers\.server_url_secret: 1"):
        _with(migration, op).downgrade()
    assert op.calls == []
    assert op.updates == []


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
        ("phone_numbers", "p1", "srv"),
        ("eval_runs", "r1", {"type": "inline", "agent": CONFIG}),
    ]
    assert op.calls == ["alter_column", "alter_column"]


def test_downgrade_with_nothing_sealed_needs_no_key(migration, no_key):
    op = _Op({"webhook_subscriptions": [("w1", "plaintext")]})
    _with(migration, op).downgrade()
    assert op.calls == ["alter_column", "alter_column"]
    assert op.updates == []
