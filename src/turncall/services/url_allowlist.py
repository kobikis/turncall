"""Shared outbound-URL allowlist.

Any URL that reaches TurnCall through an agent config is an outbound target
chosen by whoever can call the API, which makes it an SSRF surface: the
request leaves from inside the network, not from the caller's machine. The
custom-LLM and S2S gateway endpoints have been gated this way from the start;
MCP server URLs are the same kind of target and now share the check.

An empty pattern list allows everything. That's the existing BYOM behaviour
and keeps local development (and a self-hosted automation server on the same
docker network) working — an operator opts in by setting
BYOM_ALLOWED_URL_PATTERNS.
"""

from __future__ import annotations

import fnmatch


def check_url_allowed(
    url: str, patterns: list[str], *, label: str = "base_url"
) -> None:
    """Raise ValueError unless `url` matches one of `patterns` (fnmatch)."""
    if not patterns:
        return
    for pattern in patterns:
        if fnmatch.fnmatch(url, pattern):
            return
    msg = f"{label} '{url}' not in allowed patterns: {patterns}"
    raise ValueError(msg)
