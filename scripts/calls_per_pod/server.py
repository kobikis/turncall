"""The pod under test for #200: the real app, on stub providers and a fake carrier.

Run it in the API image under a CPU/memory budget (`run.sh` does), and drive it
with `client.py`. Provider services run elsewhere in production, so what one pod
carries is TurnCall's own per-call work — Silero VAD, Smart Turn, resampling,
the serializer, the recorder, the database writes — and the stubs answer
instantly so that work is all that is measured. No production code path knows
the stubs exist: they replace the three provider factories, as in the harness
(#214).

Besides the app it serves two routes of its own:

- `GET /_load/setup` — the fake carrier's account and the bound number, so the
  client can sign Twilio webhooks and ring the number.
- `GET /_load/stats[?reset=1]` — process CPU seconds, RSS, the cgroup's CPU
  throttling and memory, and event-loop lag and GC pauses since the last
  reset. Lag is the
  number that becomes dead air: every call's audio is paced off this one loop.
"""

from __future__ import annotations

import asyncio
import gc
import os
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("LOAD_PORT", "8090"))
PLATFORM_KEY = "load-platform-key"
LAG_INTERVAL_S = 0.05

# One caller line and one agent reply, repeated: long enough for any run. The
# reply is one sentence, so each is one stretch of agent audio.
REPLY = "Sure, I can look that up for you right away, one moment please."
FIRST_MESSAGE = "Thanks for calling, how can I help you today?"
# Real TTS speaks about 150 words a minute.
TTS_MS_PER_WORD = 400

# The default cascade agent: every default kept (Deepgram/OpenAI/Deepgram,
# Silero VAD, Smart Turn V3, barge-in, idle guard, recording). Analysis is off
# only because it runs after the call on a real provider the stubs don't cover.
AGENT: dict[str, Any] = {
    "system_prompt": "You are a helpful support agent.",
    "first_message": FIRST_MESSAGE,
    "analysis": {"enabled": False},
}


def _import_harness_without_pytest() -> None:
    """Register `tests` and `tests.harness` as bare packages.

    `tests/harness/__init__.py` imports its pytest-facing `app` module, and a
    pod image has no pytest (nor should its memory figure include it). With
    the packages registered, importing `tests.harness.stubs` loads only that
    module.
    """
    for name, path in (
        ("tests", ROOT / "tests"),
        ("tests.harness", ROOT / "tests/harness"),
    ):
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__path__ = [str(path)]
            sys.modules[name] = module


class GCPauses:
    """How long each garbage collection stopped the process — a full (gen 2)
    pass over a heap holding every call's pipeline stops the loop for all of
    them at once, which no CPU figure shows."""

    def __init__(self) -> None:
        self.pauses: list[tuple[int, float]] = []  # (generation, seconds)
        self._started = 0.0
        gc.callbacks.append(self._on_gc)

    def _on_gc(self, phase: str, info: dict[str, int]) -> None:
        if phase == "start":
            self._started = time.perf_counter()
        else:
            self.pauses.append(
                (info["generation"], time.perf_counter() - self._started)
            )

    def summary(self, reset: bool) -> dict[str, float]:
        full = [s for g, s in self.pauses if g == 2]
        every = [s for _, s in self.pauses]
        if reset:
            self.pauses = []
        return {
            "collections": len(every),
            "total_ms": round(sum(every) * 1000, 1),
            "max_ms": round(max(every, default=0) * 1000, 1),
            "full": len(full),
            "full_max_ms": round(max(full, default=0) * 1000, 1),
        }


class LagMonitor:
    """How late a 50ms sleep wakes up: event-loop lag, sampled continuously."""

    def __init__(self) -> None:
        self.samples: list[float] = []

    async def run(self) -> None:
        while True:
            start = time.monotonic()
            await asyncio.sleep(LAG_INTERVAL_S)
            self.samples.append(max(0.0, time.monotonic() - start - LAG_INTERVAL_S))

    def summary(self, reset: bool) -> dict[str, float]:
        samples = sorted(self.samples)
        if reset:
            self.samples = []
        if not samples:
            return {"count": 0, "p50_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0}

        def pct(p: float) -> float:
            return round(
                samples[min(len(samples) - 1, int(p * len(samples)))] * 1000, 1
            )

        return {
            "count": len(samples),
            "p50_ms": pct(0.50),
            "p99_ms": pct(0.99),
            "max_ms": round(samples[-1] * 1000, 1),
        }


def _read_kv(path: str) -> dict[str, int]:
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return {}
    return {k: int(v) for k, v in (line.split()[:2] for line in lines if line.strip())}


def _rss_bytes() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return 0


def _cgroup() -> dict[str, int]:
    cpu = _read_kv("/sys/fs/cgroup/cpu.stat")
    try:
        memory = int(Path("/sys/fs/cgroup/memory.current").read_text())
    except (OSError, ValueError):
        memory = 0
    return {
        "usage_usec": cpu.get("usage_usec", 0),
        "nr_periods": cpu.get("nr_periods", 0),
        "nr_throttled": cpu.get("nr_throttled", 0),
        "throttled_usec": cpu.get("throttled_usec", 0),
        "memory_bytes": memory,
    }


def _loop_thread_cpu_seconds() -> float:
    """CPU time of the main thread alone — the event loop every call shares.
    It saturates long before a multi-core pod does."""
    fields = (
        Path(f"/proc/self/task/{os.getpid()}/stat")
        .read_text()
        .rsplit(")", 1)[1]
        .split()
    )
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def _stats(lag: LagMonitor, pauses: GCPauses, reset: bool) -> dict[str, Any]:
    times = os.times()
    return {
        "wall": time.time(),
        "cpu_seconds": times.user + times.system,
        "loop_cpu_seconds": _loop_thread_cpu_seconds(),
        "rss_bytes": _rss_bytes(),
        "cgroup": _cgroup(),
        "lag": lag.summary(reset),
        "gc": pauses.summary(reset),
    }


async def _setup(base_url: str, carrier: Any) -> dict[str, str]:
    """A project, its admin key, the default agent and a bound number — made
    through the API, as the builder would."""
    import uuid

    import httpx

    async with httpx.AsyncClient(base_url=base_url, timeout=30) as http:
        headers = {"X-Platform-Key": PLATFORM_KEY}
        project = await http.post(
            "/v1/projects", json={"name": "load"}, headers=headers
        )
        project.raise_for_status()
        key = await http.post(
            "/v1/api-keys",
            params={"project_id": project.json()["data"]["id"]},
            json={"name": "load", "role": "admin"},
            headers=headers,
        )
        key.raise_for_status()
        http.headers["Authorization"] = f"Bearer {key.json()['data']['raw_key']}"
        agent = await http.post("/v1/agents", json={"name": "load", "config": AGENT})
        agent.raise_for_status()
        e164 = "+1555" + str(uuid.uuid4().int)[:7]
        number = carrier.add_number(e164)
        bound = await http.post(
            "/v1/phone-numbers",
            json={
                "e164_number": e164,
                "external_number_sid": number.sid,
                "routing_target_type": "agent",
                "routing_target_id": agent.json()["data"]["id"],
            },
        )
        bound.raise_for_status()
    return {
        "account_sid": carrier.account_sid,
        "auth_token": carrier.auth_token,
        "to_number": e164,
        "voice_url": carrier.number(e164).voice_url,
        "status_callback_url": carrier.number(e164).status_callback_url,
    }


async def main() -> None:
    _import_harness_without_pytest()
    from tests.harness.carrier import FakeCarrier
    from tests.harness.stubs import Script, install_stub_providers

    carrier = FakeCarrier()
    public_url = os.environ.get("LOAD_PUBLIC_BASE_URL", f"http://127.0.0.1:{PORT}")
    os.environ.update(
        {
            "TWILIO_ACCOUNT_SID": carrier.account_sid,
            "TWILIO_AUTH_TOKEN": carrier.auth_token,
            # Present so the missing-key check passes; the stubs never send them.
            "DEEPGRAM_API_KEY": "load-stub",
            "OPENAI_API_KEY": "load-stub",
            "PUBLIC_BASE_URL": public_url,
            "PLATFORM_API_KEY": PLATFORM_KEY,
        }
    )
    os.environ.setdefault(
        "LOCAL_STORAGE_PATH", tempfile.mkdtemp(prefix="turncall-load-")
    )
    # Development tracing exports every span to the console; a pod exports to a
    # collector instead, which this measurement does not include.
    os.environ.setdefault("PIPECAT_ENABLE_TRACING", "false")

    import uvicorn
    from fastapi import FastAPI

    from turncall.app import create_app
    from turncall.config.settings import get_settings

    get_settings.cache_clear()
    install_stub_providers(
        Script(
            caller=("I'd like to check on my order.",) * 10_000,
            agent=(REPLY,) * 10_000,
            tts_ms_per_word=TTS_MS_PER_WORD,
        )
    )
    carrier.install()

    lag, pauses = LagMonitor(), GCPauses()
    setup: dict[str, str] = {}
    app: FastAPI = create_app()

    @app.get("/_load/stats", include_in_schema=False)
    async def stats(reset: int = 0) -> dict[str, Any]:
        return _stats(lag, pauses, bool(reset))

    @app.get("/_load/setup", include_in_schema=False)
    async def get_setup() -> dict[str, str]:
        return setup

    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=PORT, lifespan="on")
    )
    serving = asyncio.create_task(server.serve())
    monitor = asyncio.create_task(lag.run())
    while not server.started:
        if serving.done():
            raise SystemExit("the app failed to start; see the log")
        await asyncio.sleep(0.05)
    setup.update(await _setup(f"http://127.0.0.1:{PORT}", carrier))
    print(f"load pod ready: {setup['to_number']} on {public_url}", flush=True)
    await serving
    monitor.cancel()


if __name__ == "__main__":
    import uvloop

    # What `uvicorn.run` picks in production: uvloop when it is installed.
    asyncio.run(main(), loop_factory=uvloop.new_event_loop)
