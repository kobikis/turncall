"""Shared outbound-URL allowlist.

Any URL that reaches TurnCall through an agent config is an outbound target
chosen by whoever can call the API, which makes it an SSRF surface: the
request leaves from inside the network, not from the caller's machine. The
custom-LLM and S2S gateway endpoints have been gated this way from the start;
MCP server URLs are the same kind of target and share the check.

An empty pattern list allows everything. That's the existing BYOM behaviour
and keeps local development (and a self-hosted automation server on the same
docker network) working — an operator opts in by setting
BYOM_ALLOWED_URL_PATTERNS.
"""

from __future__ import annotations

import fnmatch
from urllib.parse import urlsplit


def _hostname(value: str) -> str | None:
    """The host of a URL or a URL-shaped pattern, or None if it has neither.

    Patterns are allowed to be looser than URLs — a bare `*` has no host at
    all — so this reports absence rather than guessing.
    """
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


def check_url_allowed(
    url: str, patterns: list[str], *, label: str = "base_url"
) -> None:
    """Raise ValueError unless `url` matches one of `patterns`.

    A pattern has to match twice: the whole URL, and — when the pattern names
    a host — the URL's host on its own.

    The second check is the one that matters. fnmatch's `*` spans `/`, so
    `https://*.trusted.com/*` also matched `https://evil.com/x.trusted.com/y`
    and `https://evil.com/?u=https://a.trusted.com/`: the attacker puts the
    trusted name somewhere in the path or query and the request still leaves
    for their host. Comparing hosts separately removes the separator to slip
    past, and `hostname` ignores any `user@` prefix, so
    `https://api.trusted.com@evil.com/` is judged on `evil.com`.

    Patterns that name no host (`*`, `*openai*`) keep matching on the whole
    URL alone — they were always blunt instruments and narrowing them would
    break configs that rely on them.
    """
    if not patterns:
        return

    url_host = (_hostname(url) or "").lower()

    for pattern in patterns:
        if not fnmatch.fnmatch(url, pattern):
            continue
        pattern_host = _hostname(pattern)
        if pattern_host and not fnmatch.fnmatch(url_host, pattern_host.lower()):
            continue
        return

    msg = f"{label} '{url}' not in allowed patterns: {patterns}"
    raise ValueError(msg)
