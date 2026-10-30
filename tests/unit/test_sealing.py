"""Secrets sealed at rest (#338): the cipher, the walkers, the column types.

The walkers are held to `sanitize_config`, to the domain models themselves, and
to the frozen copy in the seal_secrets_at_rest migration, so a secret field
added later is caught whichever list it was left out of — and the rows the
migration sealed are the ones the app opens.
"""

import ast
import importlib.util
import json
import re
import types
import typing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import Column, MetaData, String, Table, Text, cast, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from turncall.config.settings import get_settings
from turncall.domain.config_secrets import (
    map_dynamic_config,
    map_inline_target,
    map_secrets,
    map_tools,
    sanitize_config,
    sanitize_tools,
)
from turncall.domain.models import AgentConfig
from turncall.storage import sealing
from turncall.storage.models import Base
from turncall.storage.repositories import credential_repo
from turncall.storage.sealing import (
    PREFIX,
    CredentialKeyError,
    SealedAgentConfig,
    SealedMetadata,
    SealedTarget,
    SealedText,
    SealedTools,
    is_sealed,
    seal,
    unseal,
)

pytestmark = pytest.mark.unit

SECRET = "sk-very-secret-value"


def _settings(key: str = "") -> Any:
    return SimpleNamespace(credentials=SimpleNamespace(encryption_key=key))


@pytest.fixture
def keyed() -> Any:
    return _settings(Fernet.generate_key().decode())


@pytest.fixture
def env_key(monkeypatch) -> str:
    """A key the column types find through `get_settings()`, as the app's do."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
    get_settings.cache_clear()
    yield key
    get_settings.cache_clear()


@pytest.fixture
def no_env_key(monkeypatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestSeal:
    def test_round_trip(self, keyed) -> None:
        sealed = seal(SECRET, keyed)
        assert sealed.startswith(PREFIX) and SECRET not in sealed
        assert is_sealed(sealed)
        assert unseal(sealed, keyed) == SECRET

    def test_no_secret_needs_no_key(self) -> None:
        assert seal("", _settings()) == ""
        assert seal(None, _settings()) is None  # type: ignore[arg-type]

    def test_sealing_without_a_key_fails_closed(self) -> None:
        with pytest.raises(CredentialKeyError, match="not set"):
            seal(SECRET, _settings())

    def test_a_malformed_key_fails_closed(self) -> None:
        with pytest.raises(CredentialKeyError, match="not a valid Fernet key"):
            seal(SECRET, _settings("not-a-key"))

    def test_another_key_cannot_read_it(self, keyed) -> None:
        sealed = seal(SECRET, keyed)
        with pytest.raises(CredentialKeyError, match="rotated"):
            unseal(sealed, _settings(Fernet.generate_key().decode()))
        with pytest.raises(CredentialKeyError, match="not set"):
            unseal(sealed, _settings())

    def test_sealing_twice_is_sealing_once(self, keyed) -> None:
        sealed = seal(SECRET, keyed)
        assert seal(sealed, keyed) == sealed

    def test_a_secret_that_only_looks_sealed_is_sealed(self, keyed) -> None:
        """A user's value starting with the prefix must not be stored as-is:
        reading it back would try to open it and fail."""
        lookalike = PREFIX + "my-own-secret"
        sealed = seal(lookalike, keyed)
        assert sealed != lookalike
        assert unseal(sealed, keyed) == lookalike

    def test_plaintext_passes_through_unseal_without_a_key(self) -> None:
        """A row from before the migration, or a test fake."""
        assert unseal(SECRET, _settings()) == SECRET

    def test_one_cipher_per_key(self, keyed) -> None:
        """Built once per key, not per value; a bad key still fails each time."""
        assert sealing.fernet(keyed) is sealing.fernet(keyed)
        other = _settings(Fernet.generate_key().decode())
        assert sealing.fernet(other) is not sealing.fernet(keyed)
        for _ in range(2):
            with pytest.raises(CredentialKeyError, match="not a valid Fernet key"):
                sealing.fernet(_settings("not-a-key"))

    def test_credential_repo_keeps_the_error(self) -> None:
        """Callers catch `credential_repo.CredentialKeyError`; it is this one."""
        assert credential_repo.CredentialKeyError is CredentialKeyError
        with pytest.raises(credential_repo.CredentialKeyError):
            seal(SECRET, _settings())


SECRETFUL = {
    "system_prompt": "hi",
    "llm": {
        "provider": "openai",
        "api_key": "sk-LEAK-llm",
        "failover": [
            {
                "provider": "anthropic",
                "api_key": "sk-LEAK-fo1",
                "failover": [{"provider": "groq", "api_key": "sk-LEAK-fo2"}],
            },
            {"provider": "openai", "api_key": None},
        ],
    },
    "aws": {
        "region": "us-east-1",
        "access_key_id": "sk-LEAK-aws-id",
        "secret_access_key": "sk-LEAK-aws",
        "session_token": "sk-LEAK-token",
    },
    "server_url": {"url": "https://x", "secret": "sk-LEAK-server"},
    "tools": [
        {"name": "book", "webhook_secret": "sk-LEAK-tool"},
        {"name": "plain", "webhook_secret": None},
    ],
    "mcp_servers": [
        {
            "name": "crm",
            "headers": {"Authorization": "sk-LEAK-header"},
            "env": {"KEY": "sk-LEAK-env", "EMPTY": ""},
        }
    ],
}


def _leaves(node: Any, path: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    if isinstance(node, dict):
        return {
            p: v
            for k, child in node.items()
            for p, v in _leaves(child, (*path, k)).items()
        }
    if isinstance(node, list):
        return {
            p: v
            for i, child in enumerate(node)
            for p, v in _leaves(child, (*path, str(i))).items()
        }
    return {path: node}


def _changed(before: Any, after: Any) -> set[tuple[str, ...]]:
    a, b = _leaves(before), _leaves(after)
    assert a.keys() == b.keys(), "a walker changed the config's shape"
    return {p for p in a if a[p] != b[p]}


def _tag(value: str) -> str:
    return "SEALED(" + value + ")"


class TestMapSecrets:
    def test_maps_every_secret_and_nothing_else(self) -> None:
        out = map_secrets(SECRETFUL, _tag)
        leaves = _leaves(out)
        assert {v for v in leaves.values() if isinstance(v, str) and "LEAK" in v} == {
            _tag(v)
            for v in _leaves(SECRETFUL).values()
            if isinstance(v, str) and "LEAK" in v
        }
        assert out["system_prompt"] == "hi"
        assert out["mcp_servers"][0]["env"]["EMPTY"] == ""
        assert out["llm"]["failover"][1]["api_key"] is None

    def test_never_mutates(self) -> None:
        before = json.dumps(SECRETFUL, sort_keys=True)
        map_secrets(SECRETFUL, _tag)
        assert json.dumps(SECRETFUL, sort_keys=True) == before

    def test_a_config_without_secrets_never_calls_fn(self) -> None:
        def boom(value: str) -> str:
            raise AssertionError(value)

        assert map_secrets({"llm": {"provider": "openai"}, "tools": []}, boom) == {
            "llm": {"provider": "openai"},
            "tools": [],
        }

    def test_non_dicts_pass_through(self) -> None:
        assert map_secrets(None, _tag) is None
        assert map_secrets([1], _tag) == [1]

    def test_inline_target(self) -> None:
        out = map_inline_target({"type": "inline", "agent": SECRETFUL}, _tag)
        assert out["agent"] == map_secrets(SECRETFUL, _tag)
        ref = {"type": "agent", "agent_id": "a", "version": 1}
        assert map_inline_target(ref, _tag) == ref

    def test_dynamic_config(self) -> None:
        meta = {"dynamic_config": SECRETFUL, "caller": {"api_key": "theirs"}}
        out = map_dynamic_config(meta, _tag)
        assert out["dynamic_config"] == map_secrets(SECRETFUL, _tag)
        # The rest of the metadata is the caller's, not a config.
        assert out["caller"] == {"api_key": "theirs"}
        assert map_dynamic_config({"lead": 1}, _tag) == {"lead": 1}


class TestParityWithMasking:
    """What is masked on the way out is what is sealed at rest, and back."""

    # Sealed, deliberately not masked: the id names the AWS key, it does not
    # unlock it, and the read keeps showing it (#338).
    SEALED_ONLY: ClassVar = {("aws", "access_key_id")}

    def test_the_same_paths(self) -> None:
        # Masking hides an empty value too; sealing has nothing to hide in one.
        empty = {p for p, v in _leaves(SECRETFUL).items() if v == ""}
        masked = _changed(SECRETFUL, sanitize_config(SECRETFUL)) - empty
        sealed = _changed(SECRETFUL, map_secrets(SECRETFUL, _tag))
        assert sealed - masked == self.SEALED_ONLY
        assert masked - sealed == set()

    def test_every_secret_field_of_the_domain_model(self) -> None:
        """Walks AgentConfig itself, so a new secret field fails here even if
        the developer adds it to neither list nor to SECRETFUL."""
        paths = _secret_paths(AgentConfig)
        assert {
            ("llm", "api_key"),
            ("llm", "failover", "[]", "api_key"),
            ("tools", "[]", "webhook_secret"),
            ("mcp_servers", "[]", "headers"),
        } <= paths
        for path in paths:
            config = _config_at(path, "sk-FIELD")
            blob = json.dumps(map_secrets(config, _tag))
            assert "sk-FIELD" in blob and '"sk-FIELD"' not in blob, path
            if tuple(p for p in path if p != "[]") not in self.SEALED_ONLY:
                assert "sk-FIELD" not in json.dumps(sanitize_config(config)), path


_SECRET_NAME = re.compile(r"api_key|.*secret.*|.*_token|access_key_id|headers|env")


def _secret_paths(
    model: type[BaseModel], path: tuple[str, ...] = (), depth: int = 0
) -> set[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()
    if depth > 3:  # LLMConfig.failover is LLMConfig: one level proves it
        return found
    for name, field in model.model_fields.items():
        for inner, listed in _unwrap(field.annotation):
            here = (*path, name, "[]") if listed else (*path, name)
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                found |= _secret_paths(inner, here, depth + 1)
            elif _SECRET_NAME.fullmatch(name):
                found.add((*path, name))
    return found


def _unwrap(annotation: Any, listed: bool = False) -> list[tuple[Any, bool]]:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        return [
            u
            for arg in typing.get_args(annotation)
            if arg is not type(None)
            for u in _unwrap(arg, listed)
        ]
    if origin is list:
        return _unwrap(typing.get_args(annotation)[0], True)
    return [(annotation, listed)]


def _config_at(path: tuple[str, ...], value: str) -> Any:
    if not path:
        return value
    head, rest = path[0], path[1:]
    if head == "[]":
        return [_config_at(rest, value)]
    if not rest and head in ("headers", "env"):
        return {head: {"NAME": value}}
    return {head: _config_at(rest, value)}


class TestColumnTypes:
    def test_text_seals_on_write_and_unseals_on_read(self, env_key) -> None:
        col = SealedText()
        stored = col.process_bind_param(SECRET, postgresql.dialect())
        assert stored is not None and stored.startswith(PREFIX)
        assert col.process_result_value(stored, postgresql.dialect()) == SECRET
        assert col.process_bind_param(None, postgresql.dialect()) is None

    def test_agent_config_round_trip(self, env_key) -> None:
        col = SealedAgentConfig()
        stored = col.process_bind_param(SECRETFUL, postgresql.dialect())
        assert "LEAK" not in json.dumps(stored)
        assert stored["system_prompt"] == "hi"
        assert col.process_result_value(stored, postgresql.dialect()) == SECRETFUL

    def test_target_and_metadata_round_trip(self, env_key) -> None:
        for col, value in (
            (SealedTarget(), {"type": "inline", "agent": SECRETFUL}),
            (SealedMetadata(), {"dynamic_config": SECRETFUL, "lead": "x"}),
        ):
            stored = col.process_bind_param(value, postgresql.dialect())
            assert "LEAK" not in json.dumps(stored)
            assert col.process_result_value(stored, postgresql.dialect()) == value

    def test_skill_tools_round_trip(self, env_key) -> None:
        """A skill's tool list (#423) is sealed and masked by the agent
        config's own rule for `tools`."""
        tools = SECRETFUL["tools"]
        col = SealedTools()
        stored = col.process_bind_param(tools, postgresql.dialect())
        assert "LEAK" not in json.dumps(stored)
        assert is_sealed(stored[0]["webhook_secret"])
        assert stored[1] == tools[1]
        assert col.process_result_value(stored, postgresql.dialect()) == tools
        assert (
            map_tools(tools, str.upper)
            == map_secrets({"tools": tools}, str.upper)["tools"]
        )
        assert sanitize_tools(tools) == sanitize_config({"tools": tools})["tools"]

    def test_a_write_without_a_secret_needs_no_key(self, no_env_key) -> None:
        plain = {"llm": {"provider": "openai"}, "system_prompt": "hi"}
        assert (
            SealedAgentConfig().process_bind_param(plain, postgresql.dialect()) == plain
        )
        assert SealedMetadata().process_bind_param({"x": 1}, postgresql.dialect()) == {
            "x": 1
        }

    def test_a_write_with_a_secret_and_no_key_fails_closed(self, no_env_key) -> None:
        with pytest.raises(CredentialKeyError):
            SealedAgentConfig().process_bind_param(SECRETFUL, postgresql.dialect())
        with pytest.raises(CredentialKeyError):
            SealedText().process_bind_param(SECRET, postgresql.dialect())

    def test_path_expressions_compile_as_on_jsonb(self) -> None:
        """takeaway_repo, ambience_repo, retention and the SMS redaction claim
        query into these columns; only the bind parameters' names differ."""

        def sql(column_type: Any) -> str:
            t = Table("agents", MetaData(), Column("config_blob", column_type))
            blob = t.c.config_blob
            stmt = select(blob["ambience"]["sound"].astext, blob[("a", "b")]).where(
                cast(blob["analysis"]["takeaway_ids"], String).contains("x"),
                blob["retention"].is_not(None),
                blob["redaction"].astext == "done",
            )
            return re.sub(
                r"%\(\w+\)s", "?", str(stmt.compile(dialect=postgresql.dialect()))
            )

        for sealed in (SealedAgentConfig(), SealedTarget(), SealedMetadata()):
            assert sql(sealed) == sql(JSONB())

    def test_the_path_reads_into_sealed_columns_are_these(self) -> None:
        """A path reads the stored value, ciphertext included (`_PlainPaths`),
        so each one into a sealed column is listed here, and none of them
        reads a secret as if it were plaintext. A new one is a test failure:
        read the whole column instead, or add it with why it is safe."""
        sealed = {
            (mapper.class_.__name__, attr.key)
            for mapper in Base.registry.mappers
            for attr in mapper.column_attrs
            if isinstance(attr.columns[0].type, (SealedText, sealing._SealedJSONB))
        }
        found = set()
        src = Path(__file__).parents[2] / "src/turncall"
        for path in src.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Attribute)
                    and isinstance(node.value.value, ast.Name)
                ):
                    continue
                column = (node.value.value.id, node.value.attr)
                if column in sealed:
                    key = node.slice
                    found.add(
                        (
                            str(path.relative_to(src)),
                            ".".join(column),
                            key.value
                            if isinstance(key, ast.Constant)
                            else ast.unparse(key),
                        )
                    )
        repos = "storage/repositories"
        assert found == {
            # No secret under any of these.
            ("services/retention.py", "AgentRow.config_blob", "retention"),
            (f"{repos}/ambience_repo.py", "AgentRow.config_blob", "ambience"),
            (f"{repos}/takeaway_repo.py", "AgentRow.config_blob", "analysis"),
            (f"{repos}/skill_repo.py", "AgentRow.config_blob", "skills"),
            (
                f"{repos}/sms_message_repo.py",
                "SmsSessionRow.metadata_json",
                "REDACTION",
            ),
            # The startup check reads the stored ciphertext on purpose.
            (f"{repos}/credential_repo.py", "CallRow.metadata_json", "dynamic_config"),
            (
                f"{repos}/credential_repo.py",
                "SmsSessionRow.metadata_json",
                "dynamic_config",
            ),
            (f"{repos}/credential_repo.py", "EvalRunRow.target", "agent"),
            (f"{repos}/credential_repo.py", "EvalScenarioRow.default_target", "agent"),
        }

    def test_a_path_is_plain_jsonb(self, env_key) -> None:
        """`col["llm"]` read back must not go through the whole-config walker,
        which would find no `llm` key in the block and leave it sealed."""
        blob = Table("a", MetaData(), Column("c", SealedAgentConfig())).c.c
        assert type(blob["llm"].type) is JSONB
        assert type(blob["llm"]["failover"].type) is JSONB
        assert type(blob[("llm", "api_key")].type) is JSONB

    def test_an_operand_binds_as_on_jsonb(self) -> None:
        """The SMS redaction claim's `metadata_json - 'claimed_until'` binds
        its key as text, as on JSONB (bound as the column, it was a JSON
        string: `jsonb - jsonb`, no such operator); a document merged in is
        still the column's, so it is sealed on the way."""
        plain = Column("c", JSONB()).op("-")("claimed_until").right.type
        for sealed in (SealedAgentConfig(), SealedTarget(), SealedMetadata()):
            col = Table("t", MetaData(), Column("c", sealed)).c.c
            assert type(col.op("-")("claimed_until").right.type) is type(plain)
            merged = col.op("||")({"dynamic_config": {}}).right.type
            assert type(merged) is type(sealed)

    def test_text_is_unbounded(self) -> None:
        """A sealed token_urlsafe(32) is ~145 characters: no String(128)."""
        assert isinstance(SealedText().impl, Text)
        assert getattr(SealedText().impl, "length", None) is None


class TestStartupCheck:
    """ensure_readable samples the sealed columns: one value per column."""

    @staticmethod
    def _session(**found: Any) -> AsyncMock:
        """What each startup query finds, by name; nothing elsewhere.

        In order: whether the schema is migrated, project credential, Twilio token, webhook secret, server_url
        secret, then a sample of documents from agents, calls, sms_sessions,
        eval_runs, eval_scenarios and skills (a tool list).
        """
        order = [
            "credential",
            "twilio",
            "webhook",
            "server_url",
            "agents",
            "calls",
            "sms_sessions",
            "eval_runs",
            "eval_scenarios",
            "skills",
        ]
        assert set(found) <= set(order)
        session = AsyncMock()
        session.scalar.side_effect = ["project_credentials"] + [
            found.get(name) for name in order
        ]
        return session

    @staticmethod
    def _sealed(keyed: Any) -> str:
        return json.dumps(map_secrets(SECRETFUL, lambda v: seal(v, keyed)))

    @pytest.mark.asyncio
    async def test_a_sealed_webhook_secret_with_no_key_fails(self, keyed) -> None:
        session = self._session(webhook=seal("whsec", keyed))
        with pytest.raises(CredentialKeyError):
            await credential_repo.ensure_readable(session, _settings())

    @pytest.mark.asyncio
    async def test_a_sealed_server_url_secret_under_another_key_fails(
        self, keyed
    ) -> None:
        session = self._session(server_url=seal("s", keyed))
        other = _settings(Fernet.generate_key().decode())
        with pytest.raises(CredentialKeyError, match="rotated"):
            await credential_repo.ensure_readable(session, other)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "column", ["agents", "calls", "sms_sessions", "eval_runs", "eval_scenarios"]
    )
    async def test_a_sealed_config_in_any_document_column_is_opened(
        self, keyed, column
    ) -> None:
        """A database whose only sealed secrets are inline configs — a call's,
        a session's, an eval target's — is checked too."""
        other = _settings(Fernet.generate_key().decode())
        with pytest.raises(CredentialKeyError, match="rotated"):
            await credential_repo.ensure_readable(
                self._session(**{column: [self._sealed(keyed)]}), other
            )
        await credential_repo.ensure_readable(
            self._session(**{column: [self._sealed(keyed)]}), keyed
        )

    @pytest.mark.asyncio
    async def test_a_sealed_skill_tool_secret_is_opened(self, keyed) -> None:
        """skills.tools is a tool list, walked as an agent config's `tools`."""
        tools = json.dumps(map_tools(SECRETFUL["tools"], lambda v: seal(v, keyed)))
        other = _settings(Fernet.generate_key().decode())
        with pytest.raises(CredentialKeyError, match="rotated"):
            await credential_repo.ensure_readable(self._session(skills=[tools]), other)
        await credential_repo.ensure_readable(self._session(skills=[tools]), keyed)

    @pytest.mark.asyncio
    async def test_a_prompt_that_mentions_the_prefix_is_no_sample(self, keyed) -> None:
        """The first match only mentions the prefix in its prompt; the next row
        holds a real sealed secret, and that one is opened."""
        prompt = json.dumps({"system_prompt": f'say "{PREFIX}"'})
        other = _settings(Fernet.generate_key().decode())
        with pytest.raises(CredentialKeyError, match="rotated"):
            await credential_repo.ensure_readable(
                self._session(agents=[prompt, self._sealed(keyed)]), other
            )

    @pytest.mark.asyncio
    async def test_only_prompts_that_mention_the_prefix_need_no_key(self) -> None:
        prompt = json.dumps({"system_prompt": f'say "{PREFIX}"'})
        await credential_repo.ensure_readable(
            self._session(agents=[prompt], calls=[prompt]), _settings()
        )

    @pytest.mark.asyncio
    async def test_nothing_sealed_needs_no_key(self) -> None:
        session = self._session()
        await credential_repo.ensure_readable(session, _settings())
        assert session.scalar.await_count == 11

    @pytest.mark.asyncio
    async def test_it_warns_when_the_key_is_unset(self, keyed) -> None:
        """The API and both workers run it, so each says so at startup."""
        seen: list[str] = []
        sink = logger.add(lambda m: seen.append(str(m)), level="WARNING")
        try:
            await credential_repo.ensure_readable(self._session(), _settings())
            await credential_repo.ensure_readable(self._session(), keyed)
        finally:
            logger.remove(sink)
        [warning] = seen
        assert "credential_encryption_key_unset" in warning
        assert "CREDENTIAL_ENCRYPTION_KEY is not set" in warning


@pytest.mark.unit
def test_every_process_that_reads_secrets_runs_the_check() -> None:
    """The warning lives in ensure_readable, so each must call it."""
    src = Path(__file__).parents[2] / "src/turncall"
    for path in ("app.py", "evals/worker.py", "campaigns/worker.py"):
        assert "credential_repo.ensure_readable(" in (src / path).read_text(), path


def test_the_prefix_is_not_base64() -> None:
    """A raw Fernet token (credential_repo's format) can never read as sealed."""
    assert ":" in PREFIX
    assert not is_sealed(Fernet(Fernet.generate_key()).encrypt(b"x").decode())
    assert sealing.PREFIX == "tcs1:"  # the migration freezes this literal


MIGRATION = (
    Path(__file__).parents[2] / "alembic/versions/e6c2a8f4b0d9_seal_secrets_at_rest.py"
)


@pytest.fixture(scope="module")
def migration() -> Any:
    spec = importlib.util.spec_from_file_location("seal_secrets_at_rest", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMatchesTheMigration:
    """The migration seals with its own frozen paths; the app must seal and
    open exactly those, or a migrated row reads back half-sealed."""

    def test_the_same_prefix(self, migration) -> None:
        assert migration.PREFIX == PREFIX

    def test_the_same_paths(self, migration) -> None:
        configs = [SECRETFUL] + [
            _config_at(path, "sk-FIELD") for path in _secret_paths(AgentConfig)
        ]
        for config in configs:
            assert map_secrets(config, _tag) == migration.map_config(config, _tag)

    def test_the_same_inline_targets_and_dynamic_configs(self, migration) -> None:
        for target in (
            {"type": "inline", "agent": SECRETFUL},
            {"agent": SECRETFUL},  # an `agent` dict is what makes it inline
            {"type": "agent", "agent_id": "a"},
            {"type": "inline", "agent": "not-a-dict"},
            None,
        ):
            assert map_inline_target(target, _tag) == migration.map_inline_target(
                target, _tag
            )
        for metadata in (
            {"dynamic_config": SECRETFUL, "lead": {"api_key": "theirs"}},
            {"lead": 1},
            {"dynamic_config": None},
            None,
        ):
            assert map_dynamic_config(metadata, _tag) == migration.map_dynamic_config(
                metadata, _tag
            )

    def test_each_opens_what_the_other_sealed(self, migration, keyed) -> None:
        cipher = Fernet(keyed.credentials.encryption_key.encode())
        assert unseal(migration.sealer(cipher)(SECRET), keyed) == SECRET
        assert migration.unsealer(cipher)(seal(SECRET, keyed)) == SECRET
