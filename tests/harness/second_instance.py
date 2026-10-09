"""A second TurnCall server, in a process of its own, on the same Postgres and
Redis as the app `run_app` started.

"Enforced across servers" (#206) cannot be shown in one interpreter: two apps
there share every module global, so a counter kept in memory would pass a
two-app test as easily as one kept in Redis. This is a separate interpreter.

It carries its own `FakeCarrier` — its dials go nowhere and are invisible to
the test, so assert on its HTTP answers — and a text LLM that answers
`STUB_TEXT_REPLY`. It runs no voice providers: place calls that need a
pipeline on the in-process app. Webhooks to it must be signed with its own URL
(`post_signed(client, carrier, f"{client.base_url}/webhooks/...", params)`).
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

STUB_TEXT_REPLY = "stub reply"
_ROOT = Path(__file__).resolve().parents[2]


def _serve(port: int) -> None:
    import uvicorn

    from tests.harness.carrier import FakeCarrier
    from turncall.app import create_app
    from turncall.services import sms_chat
    from turncall.services.llm_text import CompletionResult

    async def _reply(*_args: object, **_kwargs: object) -> CompletionResult:
        return CompletionResult(text=STUB_TEXT_REPLY, total_tokens=1)

    FakeCarrier().install()
    sms_chat.complete_text = _reply  # type: ignore[assignment]
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


@asynccontextmanager
async def second_instance(*, timeout: float = 60.0) -> AsyncIterator[httpx.AsyncClient]:
    """Start a second server with this process's environment — so enter it
    inside `run_app`, which has set that — and yield a client for it."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    log = tempfile.NamedTemporaryFile(
        prefix="turncall-second-instance-", suffix=".log", delete=False
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(_ROOT / "src"), str(_ROOT)]),
    }
    process = subprocess.Popen(  # noqa: S603 - our own interpreter and module
        [sys.executable, "-m", "tests.harness.second_instance", str(port)],
        cwd=_ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
            await _wait_until_up(client, process, Path(log.name), timeout)
            yield client
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()


async def _wait_until_up(
    client: httpx.AsyncClient,
    process: subprocess.Popen,
    log: Path,
    timeout: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"second instance exited:\n{log.read_text()[-4000:]}")
        try:
            if (await client.get("/health")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.2)
    raise TimeoutError(f"second instance not up after {timeout}s")


if __name__ == "__main__":
    _serve(int(sys.argv[1]))
