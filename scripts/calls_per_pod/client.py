"""Ramp concurrent calls against one pod and report where it degrades (#200).

Each step opens N Twilio media streams against `server.py`, each one a caller
playing recorded speech in real time and waiting for the agent's answer, holds
them for `--hold` seconds, then hangs them all up. Per step it reports what
decides a per-pod limit:

- pod CPU (of one core) and how often the cgroup throttled it, RSS;
- event-loop lag in the pod — every call's audio is paced off that one loop;
- output underruns: the caller's playout buffer (`JITTER_S` deep) running dry
  mid-utterance, i.e. a gap the person on the phone would hear;
- turn latency: end of the caller's speech to the first agent audio, which on
  stubs is the pod's own share — VAD stop, Smart Turn, pipeline, pacing;
- unanswered turns, and the client's own loop lag (if that grows, the
  measurement is the client's limit, not the pod's).

A step is `ok` while nothing is unanswered, underruns stay under
`--max-underruns-per-min` of agent audio, and p95 turn latency stays within
`--max-latency-rise-ms` of the first (least loaded) step's median. The report names the largest N
for which every step up to it was ok.

Callers are spread over worker processes (`--calls-per-proc`), so the
generator is not the bottleneck. The speech is `speech/*.wav` (macOS `say`,
8 kHz) — real speech, because Silero VAD does not take the harness's tone for
a voice and VAD + Smart Turn are most of the per-turn work.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import json
import math
import multiprocessing
import random
import sys
import time
import types
import wave
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SPEECH_DIR = Path(__file__).parent / "speech"
JITTER_S = 0.06  # caller playout buffer: an arrival later than this is heard
NEW_UTTERANCE_GAP_S = 0.5  # a longer silence starts a new agent utterance
QUIET_S = 0.6  # the agent has finished once it has been quiet this long
REPLY_TIMEOUT_S = 10.0
SPEECH_END = object()


def _import_harness_without_pytest() -> None:
    """See server.py: load `tests.harness.*` modules without pytest."""
    for name, path in (
        ("tests", ROOT / "tests"),
        ("tests.harness", ROOT / "tests/harness"),
    ):
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__path__ = [str(path)]
            sys.modules[name] = module


_import_harness_without_pytest()
sys.path.insert(0, str(ROOT / "src"))

from tests.harness.carrier import FakeCarrier  # noqa: E402
from tests.harness.twilio import SILENCE, TwilioCall, place_inbound_call  # noqa: E402


def load_speech() -> list[list[bytes]]:
    """Each WAV as 20ms mulaw frames, trimmed of leading/trailing quiet so the
    end of the last frame is the end of speech."""
    utterances = []
    for path in sorted(SPEECH_DIR.glob("*.wav")):
        with wave.open(str(path)) as wav:
            if (wav.getframerate(), wav.getsampwidth(), wav.getnchannels()) != (
                8000,
                2,
                1,
            ):
                raise SystemExit(f"{path}: want 8 kHz 16-bit mono")
            pcm = wav.readframes(wav.getnframes())
        frames = [pcm[i : i + 320] for i in range(0, len(pcm) - 319, 320)]
        loud = [i for i, f in enumerate(frames) if audioop.rms(f, 2) > 300]
        utterances.append(
            [audioop.lin2ulaw(f, 2) for f in frames[loud[0] : loud[-1] + 1]]
        )
    if not utterances:
        raise SystemExit(f"no speech in {SPEECH_DIR}")
    return utterances


@dataclass
class LoadCall(TwilioCall):
    """A harness caller that also measures what it hears and when it spoke."""

    speech_ended: list[float] = field(default_factory=list)  # wall clock
    utterance_starts: list[float] = field(default_factory=list)
    underruns: list[tuple[float, float]] = field(
        default_factory=list
    )  # (when, stall s)
    agent_audio: list[tuple[float, float]] = field(default_factory=list)  # (when, s)
    last_rx: float = 0.0  # monotonic
    _play_end: float = 0.0

    async def speak(self, frames: list[bytes]) -> float:
        """Play one utterance; returns when its last frame went out (wall)."""
        done = len(self.speech_ended)
        for frame in frames:
            await self._outgoing.put(frame)
        await self._outgoing.put(SPEECH_END)  # type: ignore[arg-type]
        while len(self.speech_ended) == done and not self.closed.is_set():
            await asyncio.sleep(0.02)
        return self.speech_ended[-1] if len(self.speech_ended) > done else time.time()

    async def _send_media(self) -> None:
        chunk = 0
        start = time.monotonic()
        last_speech = time.time()
        while True:
            try:
                payload = self._outgoing.get_nowait()
            except asyncio.QueueEmpty:
                payload = SILENCE
            if payload is SPEECH_END:
                self.speech_ended.append(last_speech)
                payload = SILENCE
            elif payload is not SILENCE:
                last_speech = time.time()
            chunk += 1
            await self._send(
                {
                    "event": "media",
                    "media": {
                        "track": "inbound",
                        "chunk": str(chunk),
                        "timestamp": str(chunk * 20),
                        "payload": base64.b64encode(payload).decode(),
                    },
                }
            )
            await asyncio.sleep(max(0.0, start + chunk * 0.02 - time.monotonic()))

    async def _receive(self) -> None:
        import websockets

        try:
            async for raw in self._ws:
                message = json.loads(raw)
                if message.get("event") == "media":
                    self._heard(
                        len(base64.b64decode(message["media"]["payload"])) / 8000
                    )
        except websockets.ConnectionClosed:
            pass
        finally:
            self.closed.set()

    def _heard(self, seconds: float) -> None:
        """A playout buffer: audio plays from first arrival + JITTER_S at real
        time; an arrival after the buffer ran dry is an underrun, and the
        buffer refills (JITTER_S again) before playing on."""
        now, wall = time.monotonic(), time.time()
        if not self.last_rx or (
            now - self.last_rx > NEW_UTTERANCE_GAP_S and now > self._play_end
        ):
            self.utterance_starts.append(wall)
            self._play_end = now + JITTER_S + seconds
        elif now > self._play_end:
            # The buffer ran dry: heard as a gap. It refills before playing on.
            self.underruns.append((wall, now - self._play_end))
            self._play_end = now + JITTER_S + seconds
        else:
            self._play_end += seconds
        self.last_rx = now
        self.agent_audio.append((wall, seconds))

    def reply_after(self, when: float) -> float | None:
        """When the first agent utterance at or after `when` (wall) began."""
        return next((s for s in self.utterance_starts if s >= when), None)

    async def wait_reply(self, after: float, timeout: float) -> bool:
        """Until an agent utterance that began after `after` (wall) has
        finished playing."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.closed.is_set():
            if self.reply_after(after) and time.monotonic() - self.last_rx > QUIET_S:
                return True
            await asyncio.sleep(0.05)
        return False


@dataclass(frozen=True)
class Window:
    start: float  # wall clock: measurements count from here...
    end: float  # ...to here


async def _one_call(
    setup: dict[str, str],
    http: Any,
    speech: list[list[bytes]],
    start_at: float,
    until: float,
    window: Window,
    rng: random.Random,
) -> dict[str, Any]:
    await asyncio.sleep(max(0.0, start_at - time.time()))
    carrier = _carrier(setup)
    try:
        call = await place_inbound_call(
            carrier,
            http,
            from_number=f"+1666{rng.randrange(10**7):07d}",
            to_number=setup["to_number"],
            call_type=LoadCall,
        )
    except Exception as exc:
        return {"connected": False, "error": f"{type(exc).__name__}: {exc}"}
    assert isinstance(call, LoadCall)
    latencies: list[float] = []
    unanswered = 0
    greeted = await call.wait_reply(0.0, 20.0)
    while greeted and time.time() < until and not call.closed.is_set():
        await asyncio.sleep(rng.uniform(0.5, 1.5))
        spoke_at = await call.speak(rng.choice(speech))
        answered = await call.wait_reply(spoke_at, REPLY_TIMEOUT_S)
        if window.start <= spoke_at <= window.end:
            if answered:
                latencies.append(call.reply_after(spoke_at) - spoke_at)
            else:
                unanswered += 1
    dropped = call.closed.is_set() and time.time() < until
    await call.hang_up()

    def within(when: float) -> bool:
        return window.start <= when <= window.end

    return {
        "connected": True,
        "greeted": greeted,
        "dropped": dropped,
        "latencies": latencies,
        "unanswered": unanswered,
        "underruns": [s for w, s in call.underruns if within(w)],
        "agent_audio_s": sum(s for w, s in call.agent_audio if within(w)),
    }


def _carrier(setup: dict[str, str]) -> FakeCarrier:
    """The client's view of the pod's fake carrier: its account and the number."""
    carrier = FakeCarrier()
    carrier.account_sid, carrier.auth_token = setup["account_sid"], setup["auth_token"]
    number = carrier.add_number(setup["to_number"])
    number.voice_url = setup["voice_url"]
    number.status_callback_url = setup["status_callback_url"]
    return carrier


async def _lag_probe(samples: list[float]) -> None:
    while True:
        start = time.monotonic()
        await asyncio.sleep(0.05)
        samples.append(time.monotonic() - start - 0.05)


async def _worker(job: dict[str, Any]) -> dict[str, Any]:
    import httpx

    speech = load_speech()
    rng = random.Random(job["seed"])  # noqa: S311 — caller timing, not security
    window = Window(job["window_start"], job["window_end"])
    lag: list[float] = []
    probe = asyncio.create_task(_lag_probe(lag))
    async with httpx.AsyncClient(timeout=30) as http:
        results = await asyncio.gather(
            *(
                _one_call(job["setup"], http, speech, at, job["until"], window, rng)
                for at in job["start_at"]
            )
        )
    probe.cancel()
    return {"calls": results, "client_lag_p99_ms": _pct(lag, 0.99) * 1000}


def run_worker(job: dict[str, Any]) -> dict[str, Any]:
    import uvloop

    return asyncio.run(_worker(job), loop_factory=uvloop.new_event_loop)


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p * len(ordered)))]


def _vm_cpu() -> tuple[int, int]:
    """(busy, total) jiffies across the whole Docker VM, from /proc/stat:
    everything else on the machine competes with the pod for these."""
    fields = [int(x) for x in Path("/proc/stat").read_text().split("\n")[0].split()[1:]]
    idle = fields[3] + fields[4]
    return sum(fields) - idle, sum(fields)


async def _get_stats(http: Any, pod: str, reset: bool = False) -> dict[str, Any]:
    response = await http.get(f"{pod}/_load/stats", params={"reset": int(reset)})
    response.raise_for_status()
    return response.json()


async def _sample_pod(http: Any, pod: str, until: float, peak: dict[str, int]) -> None:
    while time.time() < until:
        stats = await _get_stats(http, pod)
        peak["rss"] = max(peak["rss"], stats["rss_bytes"])
        peak["cgroup_mem"] = max(peak["cgroup_mem"], stats["cgroup"]["memory_bytes"])
        await asyncio.sleep(2)


async def run_step(
    args: argparse.Namespace, setup: dict[str, str], n: int
) -> dict[str, Any]:
    import httpx

    loop = asyncio.get_running_loop()
    start = time.time() + 10  # time for the worker processes to spawn and import
    window = Window(
        start + args.ramp + args.warmup, start + args.ramp + args.warmup + args.hold
    )
    rng = random.Random(n)  # noqa: S311 — caller timing, not security
    start_at = sorted(start + rng.uniform(0, args.ramp) for _ in range(n))
    procs = max(1, math.ceil(n / args.calls_per_proc))
    jobs = [
        {
            "setup": setup,
            "start_at": start_at[i::procs],
            "until": window.end,
            "window_start": window.start,
            "window_end": window.end,
            "seed": n * 1000 + i,
        }
        for i in range(procs)
    ]
    peak = {"rss": 0, "cgroup_mem": 0}
    async with httpx.AsyncClient(timeout=30) as http:
        # spawn, not fork: a child forked from inside a running loop inherits it.
        spawn = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=procs, mp_context=spawn) as pool:
            running = [loop.run_in_executor(pool, run_worker, job) for job in jobs]
            await asyncio.sleep(max(0.0, window.start - time.time()))
            first, vm_first = await _get_stats(http, args.pod, reset=True), _vm_cpu()
            await _sample_pod(http, args.pod, window.end, peak)
            last, vm_last = await _get_stats(http, args.pod), _vm_cpu()
            workers = await asyncio.gather(*running)
    await asyncio.sleep(args.cooldown)  # let the pod finalize the calls
    return _summarize(n, workers, first, last, vm_first, vm_last, peak)


def _summarize(
    n: int,
    workers: list[dict[str, Any]],
    first: dict[str, Any],
    last: dict[str, Any],
    vm_first: tuple[int, int],
    vm_last: tuple[int, int],
    peak: dict[str, int],
) -> dict[str, Any]:
    calls = [c for w in workers for c in w["calls"]]
    live = [c for c in calls if c["connected"]]
    latencies = [x for c in live for x in c["latencies"]]
    stalls = [x for c in live for x in c["underruns"]]
    audio_min = sum(c["agent_audio_s"] for c in live) / 60
    wall = last["wall"] - first["wall"]
    cg0, cg1 = first["cgroup"], last["cgroup"]
    periods = cg1["nr_periods"] - cg0["nr_periods"]
    return {
        "calls": n,
        "connected": len(live),
        "connect_errors": sorted({c["error"] for c in calls if not c["connected"]})[:3],
        "not_greeted": sum(1 for c in live if not c["greeted"]),
        "dropped": sum(1 for c in live if c["dropped"]),
        "pod_cpu_pct": round(
            100 * (last["cpu_seconds"] - first["cpu_seconds"]) / wall, 1
        ),
        "loop_cpu_pct": round(
            100 * (last["loop_cpu_seconds"] - first["loop_cpu_seconds"]) / wall, 1
        ),
        "throttled_pct": round(
            100 * (cg1["nr_throttled"] - cg0["nr_throttled"]) / periods, 1
        )
        if periods
        else 0.0,
        "rss_mb_peak": round(peak["rss"] / 2**20),
        "cgroup_mem_mb_peak": round(peak["cgroup_mem"] / 2**20),
        "loop_lag_ms": last["lag"],
        "gc_pauses": last.get("gc", {}),
        "turns": len(latencies),
        "unanswered": sum(c["unanswered"] for c in live),
        "turn_latency_ms": {
            "p50": round(_pct(latencies, 0.5) * 1000),
            "p95": round(_pct(latencies, 0.95) * 1000),
            "max": round(max(latencies, default=0) * 1000),
        },
        "underruns": len(stalls),
        "underruns_per_audio_min": round(len(stalls) / audio_min, 2)
        if audio_min
        else 0.0,
        "underrun_max_ms": round(max(stalls, default=0) * 1000),
        "agent_audio_min": round(audio_min, 1),
        "client_lag_p99_ms": round(max(w["client_lag_p99_ms"] for w in workers), 1),
        "vm_busy_cpus": round(
            (vm_last[0] - vm_first[0])
            / max(1, vm_last[1] - vm_first[1])
            * _cpu_count(),
            2,
        ),
    }


def _cpu_count() -> int:
    import os

    return os.cpu_count() or 1


def verdict(
    steps: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    """Mark each step ok or why not, against the first (least loaded) step."""
    base = steps[0]["turn_latency_ms"]["p50"] if steps else 0
    for step in steps:
        reasons = []
        if step["connected"] < step["calls"] or step["not_greeted"] or step["dropped"]:
            reasons.append("calls failed")
        if step["unanswered"]:
            reasons.append(f"{step['unanswered']} unanswered")
        if step["underruns_per_audio_min"] > args.max_underruns_per_min:
            reasons.append(f"{step['underruns_per_audio_min']} underruns/min")
        if step["turn_latency_ms"]["p95"] > base + args.max_latency_rise_ms:
            reasons.append(f"p95 latency {step['turn_latency_ms']['p95']}ms")
        if step["client_lag_p99_ms"] > 20:
            reasons.append("client-bound (results unreliable)")
        step["ok"] = not reasons
        step["why_not"] = reasons
    return steps


def capacity(steps: list[dict[str, Any]]) -> int:
    best = 0
    for step in steps:
        if not step["ok"]:
            break
        best = step["calls"]
    return best


HEADER = (
    f"{'N':>4} {'cpu%':>6} {'loop%':>6} {'thr%':>5} {'rssMB':>6} {'lag p99/max':>12} "
    f"{'gc2 n/max':>10} {'turns':>5} {'lat p50/p95':>12} {'undr/min':>8} "
    f"{'maxstall':>8} {'vmCPU':>6} {'cli':>5}  verdict"
)


def print_rows(steps: list[dict[str, Any]]) -> None:
    for s in steps:
        lag, lat, gc = s["loop_lag_ms"], s["turn_latency_ms"], s["gc_pauses"]
        print(
            f"{s['calls']:>4} {s['pod_cpu_pct']:>6} {s['loop_cpu_pct']:>6} {s['throttled_pct']:>5} "
            f"{s['rss_mb_peak']:>6} {lag['p99_ms']:>5}/{lag['max_ms']:<6} "
            f"{gc.get('full', 0):>4}/{gc.get('full_max_ms', 0):<5} "
            f"{s['turns']:>5} {lat['p50']:>5}/{lat['p95']:<6} "
            f"{s['underruns_per_audio_min']:>8} {s['underrun_max_ms']:>8} "
            f"{s['vm_busy_cpus']:>6} {s['client_lag_p99_ms']:>5}  "
            f"{'ok' if s['ok'] else '; '.join(s['why_not'])}",
            flush=True,
        )


async def main(args: argparse.Namespace) -> None:
    import httpx

    async with httpx.AsyncClient(timeout=10) as http:
        for _ in range(120):
            try:
                setup = (await http.get(f"{args.pod}/_load/setup")).json()
                if setup:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise SystemExit(f"pod at {args.pod} never became ready")
        idle = await _get_stats(http, args.pod)
    steps: list[dict[str, Any]] = []
    print(HEADER, flush=True)
    for n in args.steps:
        steps = verdict([*steps, await run_step(args, setup, n)], args)
        print_rows(steps[-1:])
        if not steps[-1]["ok"] and args.stop_on_degrade:
            break
    result = {
        "args": {k: v for k, v in vars(args).items() if k != "out"},
        "idle_rss_mb": round(idle["rss_bytes"] / 2**20),
        "steps": steps,
        "capacity": capacity(steps),
    }
    print(
        f"\ncapacity: {result['capacity']} concurrent calls (largest N with every step ok)"
    )
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--pod", default="http://127.0.0.1:8090")
    p.add_argument(
        "--steps",
        type=lambda s: [int(x) for x in s.split(",")],
        default=[1, 2, 4, 6, 8, 10, 12, 16, 20],
    )
    p.add_argument(
        "--ramp", type=float, default=10.0, help="seconds over which calls start"
    )
    p.add_argument("--warmup", type=float, default=8.0, help="seconds before measuring")
    # Two minutes is ~12 turns per call: enough that one outlier at N=1 is not
    # the p95.
    p.add_argument(
        "--hold", type=float, default=120.0, help="measured seconds per step"
    )
    p.add_argument("--cooldown", type=float, default=10.0)
    p.add_argument("--calls-per-proc", type=int, default=20)
    p.add_argument("--max-underruns-per-min", type=float, default=1.0)
    p.add_argument(
        "--max-latency-rise-ms",
        type=float,
        default=300.0,
        help="allowed p95 turn latency above the first step's median",
    )
    p.add_argument("--stop-on-degrade", action="store_true")
    p.add_argument("--out", help="write the full result as JSON here")
    return p.parse_args(argv)


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
