"""The HTTP calls the CLI makes, and nothing else.

Kept apart from the argument parsing so the exit-code logic can be tested
without a server, which is the part that matters (#77).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://localhost:8090"


class ApiError(RuntimeError):
    """The API refused, or could not be reached."""


@dataclass(frozen=True)
class Api:
    """A thin client over the eval endpoints."""

    base_url: str
    api_key: str
    timeout: float = 30.0

    @classmethod
    def from_env(cls, base_url: str | None = None, api_key: str | None = None) -> Api:
        """Flags first, then the environment, then localhost.

        `TURNCALL_API_KEY` is how CI supplies it: a key on the command line
        lands in shell history and in the job log of anything that echoes its
        own invocation.
        """
        key = api_key or os.environ.get("TURNCALL_API_KEY", "")
        if not key:
            raise ApiError("no API key: pass --api-key or set TURNCALL_API_KEY")
        return cls(
            base_url=(
                base_url or os.environ.get("TURNCALL_API_URL") or DEFAULT_BASE_URL
            ).rstrip("/"),
            api_key=key,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}/v1{path}"
        try:
            response = httpx.request(
                method,
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise ApiError(f"{method} {url} failed: {exc}") from exc

        if response.status_code >= 400:
            raise ApiError(_error_text(response))
        body = response.json()
        # The API's envelope: {"success": true, "data": ...}.
        return body.get("data", body)

    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/eval-runs", json=payload)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        return self._request("GET", f"/eval-runs/batches/{batch_id}")

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/eval-runs/{run_id}")

    def list_runs(self, **params: Any) -> list[dict[str, Any]]:
        clean = {k: v for k, v in params.items() if v is not None}
        return self._request("GET", "/eval-runs", params=clean)

    def list_scenarios(self, **params: Any) -> list[dict[str, Any]]:
        clean = {k: v for k, v in params.items() if v is not None}
        return self._request("GET", "/eval-scenarios", params=clean)


def _error_text(response: httpx.Response) -> str:
    """The API's own error, which names the offending field, over a status code."""
    try:
        body = response.json()
    except json.JSONDecodeError:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    detail = body.get("error") or body.get("detail") or body
    return f"HTTP {response.status_code}: {detail}"
