"""A real Asterisk, in Docker, driving the real app (#209).

What the fake ARI server in `tests/harness/asterisk.py` cannot prove: that a
current Asterisk accepts the ARI requests the edge makes (`externalMedia` over
chan_websocket with `transport_data=f(json)`, the bridge, `continue`), that
its `MEDIA_START` carries the `channel_id` the media route reads, and that
ulaw actually flows both ways through the bridge.

The caller is a Local channel whose other half plays a half-second tone
after a moment of silence: the harness's stub STT transcribes it as the
script's caller line. Providers are the harness's stubs,
so no provider key is needed — only Docker and a database the harness can
use (it skips on one holding credentials this process cannot read).

    pytest -m live tests/live/test_live_asterisk.py
    ASTERISK_IMAGE=andrius/asterisk:22.8-cert4_debian-trixie pytest -m live ...

Asterisk reaches the app at `host.docker.internal` (Docker Desktop; on Linux
the container is started with `--add-host=host.docker.internal:host-gateway`).
"""

# The docker CLI, found on PATH, with arguments this file builds itself.
# ruff: noqa: S603, S607

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.harness import HARNESS_AGENT, Script, ToolCall, run_app

pytestmark = pytest.mark.live

IMAGE = os.environ.get("ASTERISK_IMAGE", "andrius/asterisk:23.4.1_debian-forky")
USER, PASSWORD = "turncall", "turncall-live"
CALLER_CONTEXT = "caller"
OPERATOR = "+15559990000"


def _tool(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": name,
        "parameters_schema": {"type": "object", "properties": {}},
    }


AGENT = {
    **HARNESS_AGENT,
    "first_message": "Hello from a real Asterisk.",
    "tools": [_tool("end_call"), _tool("transfer_call")],
}


def _configs(app_port: int, number: str) -> dict[str, str]:
    return {
        "http.conf": "[general]\nenabled=yes\nbindaddr=0.0.0.0\nbindport=8088\n",
        # A phone line carries audio continuously, silence included; Wait()
        # sends nothing at all without this.
        "asterisk.conf": "[options]\ntransmit_silence=yes\n",
        "ari.conf": (
            "[general]\nenabled=yes\n\n"
            f"[{USER}]\ntype=user\nread_only=no\npassword={PASSWORD}\n"
        ),
        "websocket_client.conf": (
            "[turncall]\ntype=websocket_client\n"
            f"uri=ws://host.docker.internal:{app_port}/ws/asterisk-media\n"
            "protocols=media\nconnection_type=per_call_config\n"
            "connection_timeout=2000\n"
        ),
        "extensions.conf": (
            "[turncall-in]\n"
            f"exten => 100,1,Stasis(turncall,{number})\n same => n,Hangup()\n\n"
            # The caller: a moment of silence, then a half-second tone (the
            # stub STT's "speech"), then silence until hung up.
            f"[{CALLER_CONTEXT}]\nexten => talk,1,Answer()\n same => n,Wait(1.5)\n"
            " same => n,PlayTones(440/500,0/20000)\n same => n,Wait(30)\n\n"
            "[turncall-transfer]\n"
            "exten => _+X.,1,Answer()\n same => n,Wait(10)\n same => n,Hangup()\n"
        ),
    }


class Asterisk:
    """One container; `url` is its ARI base on the host."""

    def __init__(self, app_port: int, number: str) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="turncall-asterisk-"))
        mounts: list[str] = []
        for name, text in _configs(app_port, number).items():
            path = self._dir / name
            path.write_text(text)
            path.chmod(0o644)
            mounts += ["-v", f"{path}:/etc/asterisk/{name}:ro"]
        self.container = subprocess.run(
            [
                "docker", "run", "-d", "--rm", "-p", "127.0.0.1::8088",
                "--add-host=host.docker.internal:host-gateway",
                *mounts, IMAGE,
            ],
            check=True, capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip
        port = subprocess.run(
            ["docker", "port", self.container, "8088/tcp"],
            check=True, capture_output=True, text=True,
        ).stdout.split(":")[-1].strip()  # fmt: skip
        self.url = f"http://127.0.0.1:{port}"
        self.http = httpx.AsyncClient(
            base_url=f"{self.url}/ari", auth=(USER, PASSWORD), timeout=5
        )

    async def until(self, path: str, timeout: float = 30) -> dict:
        """GET `path` until it answers 200."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                response = await self.http.get(path)
                if response.status_code == 200:
                    return response.json()
            except httpx.TransportError:
                pass
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"Asterisk never answered {path}")
            await asyncio.sleep(0.5)

    async def call_in(self) -> str:
        """A caller dialling extension 100, who "speaks" once (a tone).
        Returns the channel id of the half that enters TurnCall's app."""
        response = await self.http.post(
            "/channels",
            params={
                "endpoint": "Local/100@turncall-in/n",
                "context": CALLER_CONTEXT,
                "extension": "talk",
                "priority": 1,
                "callerId": "15550001234",
            },
        )
        response.raise_for_status()
        return response.json()["id"]

    def logs(self) -> str:
        return subprocess.run(
            ["docker", "logs", self.container], capture_output=True, text=True
        ).stdout[-4000:]

    async def stop(self) -> None:
        await self.http.aclose()
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
        shutil.rmtree(self._dir, ignore_errors=True)


@pytest.fixture
def docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker is not running")


async def _connect(app: Any, asterisk: Asterisk) -> str:
    """Give the project this Asterisk and bind a number on it."""
    await asterisk.until("/asterisk/info")
    stored = await app.api.put(
        "/v1/credentials/asterisk",
        json={"ari_url": asterisk.url, "username": USER, "password": PASSWORD},
    )
    assert stored.status_code == 200, stored.text
    await asterisk.until("/applications/turncall", timeout=10)


async def _bind(app: Any, number: str) -> None:
    response = await app.api.post(
        "/v1/phone-numbers",
        json={
            "carrier": "asterisk",
            "e164_number": number,
            "routing_target_type": "agent",
            "routing_target_id": await app.create_agent(AGENT),
        },
    )
    assert response.status_code == 201, response.text


async def test_a_real_asterisk_call_reaches_the_agent_and_ends(
    docker, monkeypatch
) -> None:
    number = "+15551230100"
    script = Script(
        caller=("Please end the call.",),
        agent=(ToolCall("end_call", {"reason": "caller asked"}), "Goodbye!"),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        asterisk = Asterisk(int(app.base_url.rsplit(":", 1)[1]), number)
        try:
            await _bind(app, number)
            await _connect(app, asterisk)
            caller = await asterisk.call_in()
            ended = (await app.sink.wait_for("call.ended", timeout=40))["payload"]
            channels = (await asterisk.http.get("/channels")).json()
        except BaseException:
            print(asterisk.logs())
            raise
        finally:
            await asterisk.stop()

    assert ended["status"] == "completed"
    assert ended["ended_reason"] == "assistant_ended_call"
    said = [(t["role"], t["text"]) for t in ended["transcript"]]
    assert ("customer", "Please end the call.") in said
    assert ("agent", "Goodbye!") in said
    # The agent's hangup reached the caller: no channel of the call is left.
    assert caller not in {c["id"] for c in channels}


async def test_a_real_asterisk_cold_transfer_continues_in_the_dialplan(
    docker, monkeypatch
) -> None:
    from tests.integration.test_carrier_contract import _MemoryRedis

    import turncall.storage.redis as redis_module

    monkeypatch.setattr(redis_module, "get_redis", lambda: _MemoryRedis())
    number = "+15551230101"
    script = Script(
        caller=("Put me through to a person.",),
        agent=(
            ToolCall("transfer_call", {"target_number": OPERATOR}),
            "Connecting you now.",
        ),
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        asterisk = Asterisk(int(app.base_url.rsplit(":", 1)[1]), number)
        try:
            await _bind(app, number)
            await _connect(app, asterisk)
            # The half in TurnCall's app is ;2 of the Local pair.
            first = await asterisk.call_in()
            ended = (await app.sink.wait_for("call.ended", timeout=40))["payload"]
            channels = (await asterisk.http.get("/channels")).json()
        except BaseException:
            print(asterisk.logs())
            raise
        finally:
            await asterisk.stop()

    assert ended["ended_reason"] == "transferred"
    transferred = app.sink.of("call.transferred")
    assert [e["payload"]["success"] for e in transferred] == [True]
    # The caller is alive, in the transfer context, dialling the operator —
    # not hung up by TurnCall as its media channel went away.
    where = {c["dialplan"]["context"]: c for c in channels}
    assert "turncall-transfer" in where, [c["dialplan"] for c in channels]
    assert where["turncall-transfer"]["dialplan"]["exten"] == OPERATOR
    assert first in {c["id"] for c in channels}  # the caller's own half
