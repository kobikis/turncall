"""The secrets TurnCall keeps are sealed at rest, and still work (#338).

Through the running app (the harness), a secret goes in where a customer puts
it: a webhook subscription, a number's `server_url`, an agent's config, the
inline agent a call-init server answers with, an eval scenario's and run's
inline target. Then two things are checked. The database holds no plaintext
copy of any of them, read with raw SQL past the column types, every table, and
each value in its column is `tcs1:` ciphertext. And everything that uses one
still gets the plaintext: webhook deliveries and call-init requests verify
with the secret the API handed out, the tool webhook and the MCP server get
theirs, and the LLM service gets its key. An API read still masks.

Without CREDENTIAL_ENCRYPTION_KEY, a write holding a secret is a 503 and a
write holding none goes through.
"""

import asyncio
import json
import secrets
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from aiohttp import web

from tests.harness import HARNESS_AGENT, Script, ToolCall, place_inbound_call, run_app
from turncall.config.settings import Settings, get_settings
from turncall.domain.config_secrets import MASK, map_secrets
from turncall.events.webhook_signing import verify_signature
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.models import AgentRow, EvalRunRow, SmsSessionRow
from turncall.storage.repositories import sms_session_repo
from turncall.storage.sealing import PREFIX, is_sealed, unseal

pytestmark = pytest.mark.integration

CALLER = "+15550004444"
SCRIPTED = {"turns": [{"user": "hello", "expect": [{"event": "llm_response"}]}]}


def _secret(kind: str) -> str:
    """A secret no other row in the database can hold."""
    return f"{kind}-{secrets.token_hex(16)}"


@dataclass
class Request:
    path: str
    headers: dict[str, str]
    body: str

    def signed_with(self, secret: str) -> bool:
        return verify_signature(
            self.body,
            secret,
            self.headers.get("X-TurnCall-Signature", ""),
            int(self.headers.get("X-TurnCall-Timestamp", "0")),
        )


@dataclass
class Endpoint:
    """A customer's HTTP endpoint: records every POST, answers `reply`."""

    reply: dict[str, Any] = field(default_factory=lambda: {"ok": True})
    requests: list[Request] = field(default_factory=list)
    url: str = ""
    _runner: web.AppRunner | None = None
    _arrived: asyncio.Event = field(default_factory=asyncio.Event)

    async def __aenter__(self) -> "Endpoint":
        app = web.Application()
        app.router.add_post("/{path:.*}", self._answer)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _answer(self, request: web.Request) -> web.Response:
        self.requests.append(
            Request(request.path, dict(request.headers), await request.text())
        )
        self._arrived.set()
        return web.json_response(self.reply)

    async def wait(self, *, timeout: float = 20.0) -> Request:
        await asyncio.wait_for(self._arrived.wait(), timeout)
        return self.requests[0]


class _RecordingHeaders:
    """ASGI middleware: what every request to the MCP server carried."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.headers: list[dict[str, str]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            self.headers.append(
                {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            )
        await self.app(scope, receive, send)


@pytest.fixture(scope="module")
def mcp_probe() -> Any:
    """A real MCP server over streamable HTTP, recording its request headers."""
    import uvicorn

    try:
        from mcp.server.mcpserver import MCPServer as Server
    except ModuleNotFoundError:
        from mcp.server.fastmcp import FastMCP as Server

    server = Server(name="probe")

    @server.tool()
    def add(a: int, b: int) -> str:
        """Add two numbers."""
        return f"sum={a + b}"

    app = _RecordingHeaders(server.streamable_http_app())
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])

    def serve() -> None:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

    threading.Thread(target=serve, daemon=True).start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return SimpleNamespace(url=f"http://127.0.0.1:{port}/mcp", app=app)
        time.sleep(0.1)
    pytest.skip("MCP probe server did not start")  # pragma: no cover


async def _sql(query: str, **params: Any) -> list[Any]:
    """Rows as stored: raw SQL, past every column type."""
    engine = create_engine(Settings().database)
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(sa.text(query), params)).all())
    finally:
        await engine.dispose()


def _doc(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def _plaintext_in(needles: list[str]) -> dict[str, list[str]]:
    """Each needle some row holds in plaintext, and the tables that hold it."""
    engine = create_engine(Settings().database)
    found: dict[str, list[str]] = {}
    try:
        async with engine.connect() as conn:
            tables = (
                await conn.execute(
                    sa.text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                    )
                )
            ).scalars()
            for table in list(tables):
                # The name is a table's own, from pg_tables.
                query = (
                    "SELECT n FROM unnest(CAST(:needles AS text[])) AS n "  # noqa: S608
                    f'WHERE EXISTS (SELECT 1 FROM "{table}" AS r '
                    "WHERE strpos(r::text, n) > 0)"
                )
                hits = await conn.execute(sa.text(query), {"needles": needles})
                for needle in hits.scalars():
                    found.setdefault(needle, []).append(table)
    finally:
        await engine.dispose()
    return found


def _secrets_in(config: dict[str, Any]) -> list[str]:
    """Every secret a config holds, as stored, by the app's own walker."""
    held: list[str] = []

    def collect(value: str) -> str:
        held.append(value)
        return value

    map_secrets(config, collect)
    return held


def _assert_sealed(config: dict[str, Any], plaintext: list[str]) -> None:
    """Each of the config's secrets is ciphertext that opens to one given."""
    stored = _secrets_in(config)
    assert len(stored) == len(plaintext), stored
    assert all(is_sealed(v) for v in stored), stored
    assert sorted(unseal(v) for v in stored) == sorted(plaintext)


async def _bind_to_call_init(app: Any, server_url: str) -> dict[str, Any]:
    """A number routed to `server_url`; the bind's response."""
    e164 = "+1555" + str(uuid.uuid4().int)[:7]
    number = app.carrier.add_number(e164)
    r = await app.api.post(
        "/v1/phone-numbers",
        json={
            "e164_number": e164,
            "external_number_sid": number.sid,
            "routing_target_type": "webhook",
            "server_url": server_url,
        },
    )
    assert r.status_code == 201, r.text
    return dict(r.json()["data"])


async def test_a_call_gets_the_secrets_its_rows_hold_sealed(monkeypatch, mcp_probe):
    """Webhook deliveries, call-init, the tool webhook, the MCP server and the
    LLM each get the plaintext of a secret stored only as ciphertext."""
    from turncall.orchestrator import pipeline_factory

    llm_key, tool_secret = _secret("sk-inline"), _secret("tool-secret")
    mcp_token, events_secret = _secret("mcp-token"), _secret("events-secret")
    llm_keys: list[str | None] = []
    async with (
        Endpoint() as tool,
        Endpoint() as call_init,
        run_app(
            Script(
                caller=("Look it up.",), agent=(ToolCall("lookup", {}), "Found it.")
            ),
            set_env=monkeypatch.setenv,
            set_attr=monkeypatch.setattr,
        ) as app,
    ):
        stub = pipeline_factory._create_llm_service

        def _llm(config: Any, *args: Any, **kwargs: Any) -> Any:
            llm_keys.append(config.llm.api_key)
            return stub(config, *args, **kwargs)

        monkeypatch.setattr(pipeline_factory, "_create_llm_service", _llm)
        call_init.reply = {
            "agent": {
                **HARNESS_AGENT,
                "first_message": "Hello.",
                "llm": {"api_key": llm_key},
                "server_url": {
                    "url": f"{call_init.url}/events",
                    "secret": events_secret,
                },
                "tools": [
                    {
                        "name": "lookup",
                        "description": "Look it up",
                        "parameters_schema": {"type": "object", "properties": {}},
                        "webhook_url": f"{tool.url}/lookup",
                        "webhook_secret": tool_secret,
                    }
                ],
                "mcp_servers": [
                    {
                        "name": "probe",
                        "url": mcp_probe.url,
                        "headers": {"Authorization": f"Bearer {mcp_token}"},
                    }
                ],
            }
        }
        number = await _bind_to_call_init(app, f"{call_init.url}/call-init")
        number_secret = number["server_url_secret"]

        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number["e164_number"]
        )
        await call.wait_for_agent_speech(1)  # the greeting
        await call.say(400)
        invoked = await tool.wait()
        await call.wait_for_agent_speech(2)  # after the tool's result
        await call.hang_up()
        ended = await app.sink.wait_for("call.ended")

        hooks = await _sql(
            "SELECT secret FROM webhook_subscriptions WHERE project_id = :p",
            p=uuid.UUID(app.project_id),
        )
        [(stored_number_secret,)] = await _sql(
            "SELECT server_url_secret FROM phone_numbers WHERE id = :id",
            id=uuid.UUID(number["id"]),
        )
        [(dynamic,)] = await _sql(
            "SELECT metadata_json -> 'dynamic_config' FROM calls WHERE id = :id",
            id=uuid.UUID(call.call_id),
        )
        leaked = await _plaintext_in(
            [
                app.sink.secret,
                number_secret,
                llm_key,
                tool_secret,
                mcp_token,
                events_secret,
            ]
        )
        read_number = (await app.api.get(f"/v1/phone-numbers/{number['id']}")).json()
        read_hooks = (await app.api.get("/v1/webhooks")).json()
        read_call = (await app.api.get(f"/v1/calls/{call.call_id}")).json()

    assert leaked == {}, f"plaintext secrets at rest: {leaked}"

    # The subscription's secret, sealed; deliveries verify with the plaintext.
    [(stored_hook_secret,)] = hooks
    assert stored_hook_secret.startswith(PREFIX)
    assert unseal(stored_hook_secret) == app.sink.secret
    assert ended["_signature_valid"] is True
    assert app.sink.events and all(e["_signature_valid"] for e in app.sink.events)

    # The number's call-init secret, sealed; the request is signed with it.
    assert stored_number_secret.startswith(PREFIX)
    assert unseal(stored_number_secret) == number_secret
    [asked] = [r for r in call_init.requests if r.path == "/call-init"]
    assert asked.signed_with(number_secret)

    # The inline agent's secrets, sealed on the call; each user got plaintext.
    _assert_sealed(
        _doc(dynamic), [llm_key, tool_secret, f"Bearer {mcp_token}", events_secret]
    )
    assert invoked.path == "/lookup"
    assert invoked.signed_with(tool_secret)
    assert any(
        h.get("authorization") == f"Bearer {mcp_token}" for h in mcp_probe.app.headers
    ), mcp_probe.app.headers
    assert llm_keys and set(llm_keys) == {llm_key}

    # Reads are as they were: the number's own secret is returned (it is the
    # caller's to verify with), a subscription's and a call's config never are.
    assert read_number["data"]["server_url_secret"] == number_secret
    assert all("secret" not in hook for hook in read_hooks["data"])
    assert "dynamic_config" not in read_call["data"]["metadata"]


async def test_stored_configs_are_sealed_and_read_masked(monkeypatch):
    """An agent's config and an eval scenario's and run's inline target hold
    every secret field sealed; reads mask them, and the rows read back through
    the ORM, as the pipeline and the eval runner read them, are plaintext."""
    monkeypatch.setenv("AWS_AGENT_CREDENTIALS_ENABLED", "true")
    held = {
        name: _secret(name)
        for name in (
            "llm-key",
            "failover-key",
            "aws-id",
            "aws-secret",
            "aws-token",
            "events-secret",
            "tool-secret",
            "mcp-header",
            "mcp-env",
        )
    }
    config = {
        **HARNESS_AGENT,
        "llm": {
            "provider": "openai",
            "api_key": held["llm-key"],
            "failover": [{"provider": "openai", "api_key": held["failover-key"]}],
        },
        "aws": {
            "region": "us-east-1",
            "access_key_id": held["aws-id"],
            "secret_access_key": held["aws-secret"],
            "session_token": held["aws-token"],
        },
        "server_url": {
            "url": "https://example.com/events",
            "secret": held["events-secret"],
        },
        "tools": [
            {
                "name": "lookup",
                "description": "Look it up",
                "parameters_schema": {"type": "object", "properties": {}},
                "webhook_url": "https://example.com/lookup",
                "webhook_secret": held["tool-secret"],
            }
        ],
        "mcp_servers": [
            {
                "name": "remote",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": held["mcp-header"]},
            },
            {
                "name": "local",
                "transport": "stdio",
                "command": "npx",
                "env": {"TOKEN": held["mcp-env"]},
            },
        ],
    }
    plaintext = list(held.values())
    inline = {"type": "inline", "agent": config}

    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        agent_id = await app.create_agent(config, name="sealed")
        scenario = await app.api.post(
            "/v1/eval-scenarios",
            json={"name": "sealed", "definition": SCRIPTED, "default_target": inline},
        )
        assert scenario.status_code == 201, scenario.text
        scenario_id = scenario.json()["data"]["id"]
        run = await app.api.post(
            "/v1/eval-runs", json={"scenario_id": scenario_id, "target": inline}
        )
        assert run.status_code == 202, run.text
        [queued] = run.json()["data"]["runs"]
        run_id = queued["id"]

        # A text session's inline agent, and the JSONB edits its row gets.
        engine = create_engine(Settings().database)
        try:
            async with create_session_factory(engine)() as session:
                sms = await sms_session_repo.create_session(
                    session,
                    project_id=uuid.UUID(app.project_id),
                    agent_id=None,
                    customer_number=CALLER,
                    turncall_number="+15550005555",
                    metadata_json={"dynamic_config": config},
                )
                await sms_session_repo.expire_session(session, sms.id)
                await sms_session_repo.release_redaction_claims(session, [sms.id])
                await session.commit()
            async with create_session_factory(engine)() as session:
                agent_row = await session.get(AgentRow, uuid.UUID(agent_id))
                run_row = await session.get(EvalRunRow, uuid.UUID(run_id))
                sms_row = await session.get(SmsSessionRow, sms.id)
                assert agent_row and run_row and sms_row
                orm = {
                    "agent": agent_row.config_blob,
                    "run": run_row.target["agent"],
                    "sms": sms_row.metadata_json["dynamic_config"],
                }
        finally:
            await engine.dispose()

        [(blob,)] = await _sql(
            "SELECT config_blob FROM agents WHERE id = :id", id=uuid.UUID(agent_id)
        )
        [(default_target,)] = await _sql(
            "SELECT default_target FROM eval_scenarios WHERE id = :id",
            id=uuid.UUID(scenario_id),
        )
        [(target,)] = await _sql(
            "SELECT target FROM eval_runs WHERE id = :id", id=uuid.UUID(run_id)
        )
        [(sms_metadata,)] = await _sql(
            "SELECT metadata_json FROM sms_sessions WHERE id = :id", id=sms.id
        )
        leaked = await _plaintext_in(plaintext)
        read_agent = (await app.api.get(f"/v1/agents/{agent_id}")).json()["data"]
        read_scenario = (await app.api.get(f"/v1/eval-scenarios/{scenario_id}")).json()
        read_run = (await app.api.get(f"/v1/eval-runs/{run_id}")).json()["data"]

    # The audit log keeps an agent change as a read shows it, and a read shows
    # the AWS key id: it names the key, it does not unlock it.
    assert leaked == {held["aws-id"]: ["audit_log"]}, leaked
    _assert_sealed(_doc(blob), plaintext)
    _assert_sealed(_doc(default_target)["agent"], plaintext)
    _assert_sealed(_doc(target)["agent"], plaintext)
    _assert_sealed(_doc(sms_metadata)["dynamic_config"], plaintext)
    for read in orm.values():
        assert sorted(_secrets_in(read)) == sorted(plaintext)

    # The reads mask every secret but the AWS key id, which names the key.
    for shown in (
        read_agent["config"],
        read_scenario["data"]["default_target"]["agent"],
        read_run["target"]["agent"],
    ):
        assert sorted(_secrets_in(shown)) == sorted([held["aws-id"]] + [MASK] * 8)


async def test_without_the_key_only_a_write_holding_a_secret_is_refused(monkeypatch):
    """No key, no plaintext fallback: 503 `credential_key_unavailable` for a
    write that holds a secret, and nothing written; any other write works."""
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        key = get_settings().credentials.encryption_key
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "")
        get_settings.cache_clear()
        try:
            hook = await app.api.post(
                "/v1/webhooks", json={"url": app.sink.url, "events": ["*"]}
            )
            keyed_agent = await app.api.post(
                "/v1/agents",
                json={
                    "name": "keyed",
                    "config": {**HARNESS_AGENT, "llm": {"api_key": _secret("sk")}},
                },
            )
            plain_agent = await app.api.post(
                "/v1/agents", json={"name": "plain", "config": HARNESS_AGENT}
            )
            e164 = "+1555" + str(uuid.uuid4().int)[:7]
            number = app.carrier.add_number(e164)
            with_server_url = await app.api.post(
                "/v1/phone-numbers",
                json={
                    "e164_number": e164,
                    "external_number_sid": number.sid,
                    "routing_target_type": "webhook",
                    "server_url": "https://example.com/call-init",
                },
            )
            plain_number = await app.bind_number(plain_agent.json()["data"]["id"])
        finally:
            monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
            get_settings.cache_clear()
        stored = await _sql(
            "SELECT"
            " (SELECT count(*) FROM webhook_subscriptions WHERE project_id = :p),"
            " (SELECT count(*) FROM agents WHERE project_id = :p),"
            " (SELECT count(*) FROM phone_numbers WHERE project_id = :p)",
            p=uuid.UUID(app.project_id),
        )

    for refused in (hook, keyed_agent, with_server_url):
        assert refused.status_code == 503, refused.text
        body = refused.json()
        assert body["code"] == "credential_key_unavailable"
        assert "CREDENTIAL_ENCRYPTION_KEY" in body["error"]
    assert plain_agent.status_code == 201, plain_agent.text
    assert plain_number
    # Only the harness's own subscription, the plain agent and plain number.
    assert stored == [(1, 1, 1)]
