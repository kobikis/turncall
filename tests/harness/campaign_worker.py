"""A real `turncall-campaign-worker` process, to be killed mid-dial (#232).

"A worker that dies loses nothing and dials nothing twice" is a claim about a
process dying with a transaction open and a carrier request in flight, so it
is shown with one: `run_worker()`, the entrypoint's own body, in a child
interpreter on the test's Postgres and Redis, killed with SIGKILL.

The child carries its own fake carrier, and every `dial` it receives is
appended to a JSON-lines file the moment the request arrives — before the
carrier answers — so the parent counts what the carrier was asked even when the
worker never lived to record it. A dial's line has the same params as
`FakeCarrier`'s, so `CarrierAction`s rebuilt from it work with `post_signed`.

`stall_at` names where the child stops, forever, on the `nth` dial of
`project_id`'s from `from_number` (a pass serves every campaign in the
database, so other campaigns' dials are recorded but never stall it):

- `claimed`  — the claim is committed, the carrier not yet asked;
- `carrier`  — the carrier has the request and has not answered;
- `answered` — the carrier answered with a call SID, nothing committed yet;
- `never`    — a worker that just works.

On stalling it appends `{"kind": "stalled"}`, the parent's cue to kill it.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from tests.harness.carrier import CarrierAction, FakeCarrier

_ROOT = Path(__file__).resolve().parents[2]
STALLS = ("claimed", "carrier", "answered", "never")


class WorkerProcess:
    """One child worker and the file its carrier writes to."""

    def __init__(
        self,
        record: Path,
        *,
        project_id: str,
        from_number: str,
        stall_at: str = "never",
        nth: int = 1,
        env: dict[str, str] | None = None,
    ) -> None:
        assert stall_at in STALLS, stall_at
        self.record = record
        self.from_number = from_number
        self._log = tempfile.NamedTemporaryFile(
            prefix="turncall-campaign-worker-", suffix=".log", delete=False
        )
        self.process = subprocess.Popen(  # noqa: S603 - our own interpreter and module
            [
                sys.executable,
                "-m",
                "tests.harness.campaign_worker",
                str(record),
                project_id,
                from_number,
                stall_at,
                str(nth),
            ],
            cwd=_ROOT,
            env={
                **os.environ,
                **(env or {}),
                "PYTHONPATH": os.pathsep.join([str(_ROOT / "src"), str(_ROOT)]),
            },
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def lines(self) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def dials(self) -> list[CarrierAction]:
        """Every dial the child's carrier received from `from_number`."""
        return [
            CarrierAction("dial", line["account_sid"], line["params"])
            for line in self.lines()
            if line["kind"] == "dial"
            and line["params"]["from_number"] == self.from_number
        ]

    async def wait_until_stalled(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while not any(line["kind"] == "stalled" for line in self.lines()):
            if self.process.poll() is not None:
                raise RuntimeError(f"worker exited early; see {self._log.name}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"worker never stalled; see {self._log.name}")
            await asyncio.sleep(0.05)

    def kill(self) -> None:
        """SIGKILL: no handler, no `finally`, no rollback — the process is gone."""
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(timeout=10)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.kill()


# --- the child ---------------------------------------------------------------


def _append(record: Path, line: dict[str, Any]) -> None:
    with record.open("a") as f:
        f.write(json.dumps(line) + "\n")
        f.flush()
        os.fsync(f.fileno())


async def _stall(record: Path) -> None:
    _append(record, {"kind": "stalled"})
    await asyncio.Event().wait()


def _install(
    record: Path, project_id: str, from_number: str, stall_at: str, nth: int
) -> None:
    from turncall.storage.repositories import campaign_repo, suppression_repo

    ours = project_id
    seen = {"claimed": 0, "dials": 0}
    answered = asyncio.Event()

    class _RecordingCarrier(FakeCarrier):
        async def record(self, kind: str, account_sid: str, **params: Any) -> None:
            _append(
                record, {"kind": kind, "account_sid": account_sid, "params": params}
            )
            if kind != "dial" or params["from_number"] != from_number:
                return
            seen["dials"] += 1
            if seen["dials"] == nth and stall_at == "carrier":
                await _stall(record)
            if seen["dials"] == nth and stall_at == "answered":
                answered.set()

    is_suppressed = suppression_repo.is_suppressed
    record_dial = campaign_repo.record_dial

    async def _is_suppressed(session: Any, *, project_id: Any, number: str) -> bool:
        # The first thing a dial does after its claim commits.
        if stall_at == "claimed" and str(project_id) == ours:
            seen["claimed"] += 1
            if seen["claimed"] == nth:
                await _stall(record)
        return await is_suppressed(session, project_id=project_id, number=number)

    async def _record_dial(*args: Any, **kwargs: Any) -> bool:
        # The carrier's SID is in hand; the call row and the target's move
        # are about to commit.
        if answered.is_set():
            await _stall(record)
        return await record_dial(*args, **kwargs)

    _RecordingCarrier().install()
    suppression_repo.is_suppressed = _is_suppressed  # type: ignore[assignment]
    campaign_repo.record_dial = _record_dial  # type: ignore[assignment]


def _main() -> None:
    from turncall.campaigns.worker import run_worker

    record, project_id, from_number, stall_at, nth = sys.argv[1:6]
    _install(Path(record), project_id, from_number, stall_at, int(nth))
    asyncio.run(run_worker())


if __name__ == "__main__":
    _main()
