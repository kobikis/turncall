"""A delivery to a plain http:// URL the write gate would now refuse is
logged, not stopped (#340).

Five kinds of delivery send call data out: webhook subscriptions, call-init,
the other server events, tool webhooks and MCP servers. A URL stored before
the gate, or an inline config, can still be plain http where it is no longer
allowed. Each such delivery logs `plain_http_delivery` (once a minute per
kind, the host only) and still goes out, and nothing about the warning can
stop it.
"""

import uuid
from collections.abc import Awaitable, Callable

import httpx
import pytest
from loguru import logger

from tests.conftest import mcp_settings
from turncall.config.settings import Settings, get_settings
from turncall.domain.models import MCPServerConfig, ToolDefinition
from turncall.events import server_events, webhook_delivery
from turncall.events.server_events import (
    ServerEventRequest,
    ServerEventType,
    send_server_event,
)
from turncall.events.webhook_delivery import WebhookEvent, deliver_webhook
from turncall.services import outbound_url, tool_webhook
from turncall.services.mcp_client import MCPSessionManager
from turncall.services.tool_webhook import post_tool_webhook
from turncall.services.webhook_verification import RefusalLog

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

PLAIN = "http://crm.example.com/hook/s3cret-path?token=s3cret-query"
IN_CLUSTER = "http://turncall-agent-9001.turncall.svc.cluster.local:8000/events"
KINDS = ["webhook", "call_init", "server_event", "tool", "mcp"]

Deliver = Callable[[str], Awaitable[None]]


class _Client:
    """Stands in for the shared httpx client; records what was posted."""

    def __init__(self, sent: list[str]) -> None:
        self._sent = sent

    async def post(self, url: str, **_: object) -> httpx.Response:
        self._sent.append(url)
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))


@pytest.fixture
def env(monkeypatch):
    """The process settings the delivery sites read, and nothing from `.env`.
    A site that reads `get_settings()` itself caches it under this test's
    environment, so the cache is cleared after, before the next test."""

    def _set(turncall_env: str | None, allow: str | None = None) -> None:
        for name, value in (
            ("TURNCALL_ENV", turncall_env),
            ("ALLOW_PLAIN_HTTP_URLS", allow),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        settings = Settings(_env_file=None)
        monkeypatch.setattr(outbound_url, "get_settings", lambda: settings)

    yield _set
    get_settings.cache_clear()


@pytest.fixture
def clock(monkeypatch):
    """A fresh delivery log on a clock the test moves."""
    now = [0.0]
    monkeypatch.setattr(
        outbound_url,
        "plain_http_deliveries",
        RefusalLog(
            interval=60,
            clock=lambda: now[0],
            note="more plain http deliveries of this kind since the last",
        ),
    )
    return now


@pytest.fixture
def records():
    seen: list[dict] = []
    sink = logger.add(
        lambda m: seen.append(
            {
                "level": m.record["level"].name,
                "message": m.record["message"],
                "extra": dict(m.record["extra"]),
            }
        ),
        level="INFO",
    )
    yield seen
    logger.remove(sink)


@pytest.fixture
def sent(monkeypatch) -> list[str]:
    """Every URL a delivery reached, whichever site sent it."""
    out: list[str] = []
    client = _Client(out)
    for module in (webhook_delivery, server_events, tool_webhook):
        monkeypatch.setattr(module, "get_http_client", lambda: client)
    return out


@pytest.fixture
def deliver(sent) -> Callable[[str], Deliver]:
    """The delivery site for a kind, as `await deliver(kind)(url)`."""
    project_id = uuid.uuid4()

    async def webhook(url: str) -> None:
        event = WebhookEvent(event_type="call.ended", payload={}, project_id=project_id)
        result = await deliver_webhook(url, event, "s" * 32, max_retries=0)
        assert result.success

    async def call_init(url: str) -> None:
        event = ServerEventRequest(
            event_type=ServerEventType.CALL_INIT, call_id="c1", payload={}
        )
        response = await send_server_event(url, event, secret="s" * 32)
        assert response.success

    async def server_event(url: str) -> None:
        event = ServerEventRequest(
            event_type=ServerEventType.CALL_END, call_id="c1", payload={}
        )
        response = await send_server_event(url, event, secret="s" * 32)
        assert response.success

    async def tool(url: str) -> None:
        tool_def = ToolDefinition(
            name="book",
            description="Book it",
            parameters_schema={"type": "object", "properties": {}},
            webhook_url=url,
        )
        result = await post_tool_webhook(tool_def, {}, project_id=project_id)
        assert result == "{}"

    async def mcp(url: str) -> None:
        manager = MCPSessionManager(call_id=uuid.uuid4(), project_id=project_id)
        session = object()

        async def connect(server: MCPServerConfig) -> object:
            sent.append(server.url or "")
            return session

        manager._create_http_session = connect  # type: ignore[method-assign]
        server = MCPServerConfig(name="crm", transport="http", url=url)
        # The settings the manager is handed, as the process's are here.
        process = outbound_url.get_settings()
        settings = mcp_settings(
            environment=process.environment.value,
            allow_plain_http_urls=process.allow_plain_http_urls,
        )
        assert await manager._create_session(server, settings) is session

    sites = {
        "webhook": webhook,
        "call_init": call_init,
        "server_event": server_event,
        "tool": tool,
        "mcp": mcp,
    }
    return sites.__getitem__


def _warnings(records: list[dict]) -> list[dict]:
    return [
        r
        for r in records
        if r["level"] == "WARNING" and "plain_http_delivery" in r["message"]
    ]


@pytest.mark.parametrize("kind", KINDS)
async def test_each_site_warns_once_a_minute_and_still_delivers(
    kind, env, clock, records, sent, deliver
) -> None:
    env("production")

    for _ in range(3):
        await deliver(kind)(PLAIN)
    clock[0] = 61.0
    await deliver(kind)(PLAIN)

    assert sent == [PLAIN] * 4
    first, second = _warnings(records)
    assert (
        f"a {kind} delivery to plain http:// host crm.example.com" in (first["message"])
    )
    assert first["extra"]["route"] == kind
    assert (
        "(2 more plain http deliveries of this kind since the last)"
        in (second["message"])
    )


@pytest.mark.parametrize("kind", KINDS)
async def test_the_log_names_the_host_never_the_path_or_query(
    kind, env, clock, records, deliver
) -> None:
    env("staging")

    await deliver(kind)(PLAIN)

    (warning,) = _warnings(records)
    assert warning["extra"]["host"] == "crm.example.com"
    # Nothing logged for this delivery, by any line, carries the URL's secrets.
    assert not any(
        "s3cret" in str(r["message"]) or "s3cret" in str(r["extra"]) for r in records
    )


async def test_the_kinds_are_counted_apart(env, clock, records, deliver) -> None:
    env("production")

    for kind in KINDS:
        await deliver(kind)(PLAIN)

    assert [w["extra"]["route"] for w in _warnings(records)] == KINDS


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("url", ["https://crm.example.com/hook", IN_CLUSTER])
async def test_https_and_service_hosts_are_silent(
    kind, url, env, clock, records, sent, deliver
) -> None:
    env("production")

    await deliver(kind)(url)

    assert sent == [url]
    assert _warnings(records) == []


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    ("turncall_env", "allow"),
    [(None, None), ("development", None), ("production", "true")],
)
async def test_silent_where_plain_http_is_allowed(
    kind, turncall_env, allow, env, clock, records, sent, deliver
) -> None:
    env(turncall_env, allow)

    await deliver(kind)(PLAIN)

    assert sent == [PLAIN]
    assert _warnings(records) == []


async def test_the_opt_out_warns_in_development_too(
    env, clock, records, deliver
) -> None:
    env("development", "false")

    await deliver("webhook")(PLAIN)

    assert len(_warnings(records)) == 1


class _Broken:
    def warn(self, *_: object, **__: object) -> bool:
        raise RuntimeError("the log is broken")


@pytest.mark.parametrize("kind", KINDS)
async def test_a_failing_warning_never_stops_the_delivery(
    kind, env, records, sent, deliver, monkeypatch
) -> None:
    env("production")
    monkeypatch.setattr(outbound_url, "plain_http_deliveries", _Broken())

    await deliver(kind)(PLAIN)

    assert sent == [PLAIN]
    (failed,) = [r for r in records if r["message"] == "plain_http_warning_failed"]
    assert failed["extra"]["error"] == "RuntimeError"


async def test_mcp_reads_the_settings_it_is_handed(env, clock, records) -> None:
    # The process allows plain http; the settings the session is built with
    # do not, and they are the ones that decide.
    env("development")
    manager = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())

    async def connect(server: MCPServerConfig) -> object:
        return object()

    manager._create_http_session = connect  # type: ignore[method-assign]
    server = MCPServerConfig(name="crm", transport="http", url=PLAIN)
    await manager._create_session(server, mcp_settings(environment="production"))

    (warning,) = _warnings(records)
    assert warning["extra"]["route"] == "mcp"
