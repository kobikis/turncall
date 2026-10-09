"""The app in a process of its own, for what only a real process shows.

`run_app` shares the test's event loop and memory, which is what lets the fake
carrier and the stubs be inspected — and what makes a signal impossible to aim
at it without hitting pytest. `serve_process` boots `python -m
tests.harness.server` instead: the same stubs and fake carrier, installed for
the child's whole life, served by `uvicorn.run` exactly as `python -m turncall`
serves it in production (minus reload). A shutdown test (#199) signals it; a
load generator (#200) can drive it.

The fake carrier then lives in the child, so its recorded actions are out of
reach. The test keeps a mirror `FakeCarrier` with the same credentials: it signs
the webhooks, and `ServerProcess.bind_number` writes onto it the URLs the app
configured, so `place_inbound_call` works unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.harness.app import (
    Harness,
    WebhookSink,
    _bootstrap,
    _database_usable,
    _delete_project,
    app_env,
)
from tests.harness.asterisk import FakeAsterisk
from tests.harness.carrier import FakeCarrier
from tests.harness.stubs import Script

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ServerProcess:
    """A running app process and the harness that drives it."""

    harness: Harness
    process: asyncio.subprocess.Process
    log: Path

    async def bind_number(self, agent_id: str) -> str:
        """Bind a number through the API, mirroring the URLs it configured."""
        e164 = await self.harness.bind_number(agent_id)
        number = self.harness.carrier.number(e164)
        number.voice_url = f"{self.harness.base_url}/webhooks/twilio/voice/inbound"
        number.status_callback_url = f"{self.harness.base_url}/webhooks/twilio/status"
        return e164

    def signal(self, sig: int = signal.SIGTERM) -> None:
        self.process.send_signal(sig)

    async def exited(self, timeout: float = 30.0) -> int:
        """The exit code, waiting up to `timeout` for the process to end."""
        return await asyncio.wait_for(self.process.wait(), timeout)

    def log_tail(self, lines: int = 60) -> str:
        return "\n".join(self.log.read_text(errors="replace").splitlines()[-lines:])


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _until_live(
    http: httpx.AsyncClient, process: asyncio.subprocess.Process, log: Path
) -> None:
    for _ in range(300):
        if process.returncode is not None:
            raise RuntimeError(f"the app exited at startup:\n{log.read_text()}")
        try:
            if (await http.get("/live")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.1)
    raise TimeoutError(f"the app never answered /live:\n{log.read_text()}")


@asynccontextmanager
async def serve_process(script: Script) -> AsyncIterator[ServerProcess]:
    """Boot the app in a child process on stubs + a fake carrier.

    The script crosses the process boundary as JSON, so the agent's replies
    are text only — no `ToolCall`s.
    """
    if not all(isinstance(reply, str) for reply in script.agent):
        raise TypeError("serve_process scripts take text replies only")
    if (reason := await _database_usable()) is not None:
        pytest.skip(reason)

    carrier = FakeCarrier()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log = Path(tempfile.mkstemp(prefix="turncall-server-", suffix=".log")[1])
    env = {
        **os.environ,
        **app_env(carrier, base_url),
        "HARNESS_PORT": str(port),
        "HARNESS_SCRIPT": json.dumps(asdict(script)),
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)]),
    }
    with log.open("wb") as out:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.harness.server",
            cwd=REPO_ROOT,
            env=env,
            stdout=out,
            stderr=asyncio.subprocess.STDOUT,
        )
    sink = WebhookSink()
    project_id = ""
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            await _until_live(http, process, log)
            await sink.start()
            project_id, api_key = await _bootstrap(http)
            http.headers["Authorization"] = f"Bearer {api_key}"
            hook = await http.post(
                "/v1/webhooks", json={"url": sink.url, "events": ["*"]}
            )
            sink.secret = hook.json()["data"]["secret"]
            # Never started here: the child's ARI edge has no Asterisk to reach.
            asterisk = FakeAsterisk(
                carrier, base_url.replace("http", "ws") + "/ws/asterisk-media"
            )
            harness = Harness(base_url, http, carrier, sink, project_id, asterisk)
            yield ServerProcess(harness, process, log)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await sink.stop()
        if project_id:
            await _delete_project(project_id)


def _script_from_env() -> Script:
    raw: dict[str, Any] = json.loads(os.environ["HARNESS_SCRIPT"])
    return Script(
        caller=tuple(raw["caller"]),
        agent=tuple(raw["agent"]),
        tts_ms_per_word=raw["tts_ms_per_word"],
    )


def main() -> None:
    """The child: stubs and a fake carrier for life, then the app."""
    import uvicorn

    from tests.harness.stubs import install_stub_providers

    install_stub_providers(_script_from_env())
    carrier = FakeCarrier()
    carrier.account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    carrier.auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    carrier.install()
    uvicorn.run(
        "turncall.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=int(os.environ["HARNESS_PORT"]),
    )


if __name__ == "__main__":
    main()
