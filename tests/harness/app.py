"""The running app, on stubs and a fake carrier, plus a webhook subscriber.

`run_app` boots the real FastAPI app under uvicorn in the test's own event
loop — lifespan, real Postgres, real sockets — so the fake carrier and the
stubs share memory with the test. It bootstraps a project and an API key
through the platform endpoints, subscribes a `WebhookSink` to every event, and
deletes the project afterwards.
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import uvicorn
from aiohttp import web

from tests.harness.carrier import FakeCarrier
from tests.harness.stubs import Script, Setter, install_stub_providers

PLATFORM_KEY = "harness-platform-key"


@dataclass
class WebhookSink:
    """A subscriber endpoint: receives TurnCall's signed event webhooks."""

    secret: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    url: str = ""
    _runner: web.AppRunner | None = None
    _arrived: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/hook", self._receive)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{port}/hook"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _receive(self, request: web.Request) -> web.Response:
        from turncall.events.webhook_signing import verify_signature

        body = await request.text()
        valid = verify_signature(
            body,
            self.secret,
            request.headers.get("X-TurnCall-Signature", ""),
            int(request.headers.get("X-TurnCall-Timestamp", "0")),
        )
        self.events.append({**json.loads(body), "_signature_valid": valid})
        async with self._arrived:
            self._arrived.notify_all()
        return web.Response(status=200)

    def of(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]

    async def wait_for(self, event: str, *, timeout: float = 20.0) -> dict[str, Any]:
        """The first `event` envelope, waiting for it to be delivered."""
        async with self._arrived:
            await asyncio.wait_for(
                self._arrived.wait_for(lambda: self.of(event)), timeout
            )
        return self.of(event)[0]


@dataclass
class Harness:
    """A running app and everything a test drives it with."""

    base_url: str
    api: httpx.AsyncClient  # authenticated as the project's admin key
    carrier: FakeCarrier
    sink: WebhookSink
    project_id: str

    async def create_agent(self, config: dict[str, Any]) -> str:
        response = await self.api.post(
            "/v1/agents", json={"name": "harness", "config": config}
        )
        assert response.status_code == 201, response.text
        return str(response.json()["data"]["id"])

    async def bind_number(self, agent_id: str) -> str:
        """Own a fresh number on the carrier and bind it to the agent through
        the API — which configures its webhooks on the carrier."""
        e164 = "+1555" + str(uuid.uuid4().int)[:7]
        number = self.carrier.add_number(e164)
        response = await self.api.post(
            "/v1/phone-numbers",
            json={
                "e164_number": e164,
                "external_number_sid": number.sid,
                "routing_target_type": "agent",
                "routing_target_id": agent_id,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["data"]["twilio_webhooks_configured"] is True
        return e164


# A harness agent: the stubs answer for its providers, and what they don't
# cover (post-call analysis, the idle check-in) is off.
HARNESS_AGENT: dict[str, Any] = {
    "system_prompt": "You are the harness agent.",
    "analysis": {"enabled": False},
    "user_idle_timeout_ms": 0,
}


def app_env(carrier: FakeCarrier, base_url: str) -> dict[str, str]:
    """The environment the app runs under in the harness, in or out of process."""
    return {
        "TWILIO_ACCOUNT_SID": carrier.account_sid,
        "TWILIO_AUTH_TOKEN": carrier.auth_token,
        # Present so the missing-key check passes; the stubs never send them.
        "DEEPGRAM_API_KEY": "harness-stub",
        "OPENAI_API_KEY": "harness-stub",
        "PUBLIC_BASE_URL": base_url,
        "PLATFORM_API_KEY": PLATFORM_KEY,
        "LOCAL_STORAGE_PATH": tempfile.mkdtemp(prefix="turncall-harness-"),
        "PIPECAT_ENABLE_TRACING": "false",
    }


async def _database_usable() -> str | None:
    """Why this database cannot host the app, or None if it can."""
    from turncall.config.settings import Settings
    from turncall.storage.database import create_engine, create_session_factory
    from turncall.storage.repositories import credential_repo

    settings = Settings()
    engine = create_engine(settings.database)
    try:
        async with create_session_factory(engine)() as session:
            await credential_repo.ensure_readable(session, settings)
    except credential_repo.CredentialKeyError:
        return "database holds credentials CREDENTIAL_ENCRYPTION_KEY cannot read"
    except Exception as exc:
        return f"Postgres not reachable ({type(exc).__name__})"
    finally:
        await engine.dispose()
    return None


async def _delete_project(project_id: str) -> None:
    from sqlalchemy import delete

    from turncall.config.settings import Settings
    from turncall.storage.database import create_engine, create_session_factory
    from turncall.storage.models import ProjectRow

    engine = create_engine(Settings().database)
    async with create_session_factory(engine)() as session:
        await session.execute(
            delete(ProjectRow).where(ProjectRow.id == uuid.UUID(project_id))
        )
        await session.commit()
    await engine.dispose()


@asynccontextmanager
async def run_app(
    script: Script,
    *,
    set_env: Callable[[str, str], None],
    set_attr: Setter,
) -> AsyncIterator[Harness]:
    """Boot the app on stubs + a fake carrier; skip if Postgres can't host it.

    `set_env`/`set_attr` are pytest's `monkeypatch.setenv`/`.setattr`, so all
    of it is undone after the test. Redis is connected lazily and the call
    path does not touch it; a test that needs it checks for it itself.
    """
    from turncall import app as turncall_app
    from turncall.app import create_app
    from turncall.config.settings import get_settings

    if (reason := await _database_usable()) is not None:
        pytest.skip(reason)

    carrier = FakeCarrier()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    for name, value in app_env(carrier, base_url).items():
        set_env(name, value)
    get_settings.cache_clear()
    install_stub_providers(script, set_attr)
    carrier.install(set_attr)
    # The lifespan points loguru and stdlib logging at the sys.stdout of the
    # moment — pytest's capture, closed after this test, so every later log
    # line would be a "Logging error". Pytest captures the default sink anyway.
    set_attr(turncall_app, "setup_logging", lambda **_: None)

    server = uvicorn.Server(
        uvicorn.Config(create_app(), log_config=None, lifespan="on")
    )
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    sink = WebhookSink()
    project_id = ""
    try:
        while not server.started:
            if serving.done():
                raise RuntimeError("the app failed to start; see the log")
            await asyncio.sleep(0.05)
        await sink.start()
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            project_id, api_key = await _bootstrap(http)
            http.headers["Authorization"] = f"Bearer {api_key}"
            hook = await http.post(
                "/v1/webhooks", json={"url": sink.url, "events": ["*"]}
            )
            sink.secret = hook.json()["data"]["secret"]
            yield Harness(base_url, http, carrier, sink, project_id)
    finally:
        server.should_exit = True
        await serving
        await sink.stop()
        if project_id:
            await _delete_project(project_id)
        get_settings.cache_clear()


async def _bootstrap(http: httpx.AsyncClient) -> tuple[str, str]:
    """A project and its first (admin) key, the way the builder makes them."""
    headers = {"X-Platform-Key": PLATFORM_KEY}
    project = await http.post("/v1/projects", json={"name": "harness"}, headers=headers)
    assert project.status_code == 201, project.text
    project_id = project.json()["data"]["id"]
    key = await http.post(
        "/v1/api-keys",
        params={"project_id": project_id},
        json={"name": "harness", "role": "admin"},
        headers=headers,
    )
    assert key.status_code == 201, key.text
    return project_id, key.json()["data"]["raw_key"]
