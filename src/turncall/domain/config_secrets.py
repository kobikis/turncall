"""The secrets an agent config carries: masked on the way out, sealed at rest.

Lives in `domain/` rather than next to the agent response schema because it has
more than one exit to guard: the agent endpoints, an eval run's snapshot, and
the eval webhook payloads (#91). A copy per exit is how the eval paths shipped
without one.

The `map_*` walkers name the same fields for the other direction: storage seals
each one before a config reaches a row and unseals it on the way back (#338,
`storage/sealing.py`). They are pure — the function applied is the caller's —
so no crypto lives in `domain/`. A test holds the two lists to each other, so a
field masked but not sealed (or sealed but not masked) fails the suite.
"""

from collections.abc import Callable
from typing import Any

MASK = "***"


def sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Mask every secret in an agent config before returning it in API responses.

    A read-only key must not be able to exfiltrate credentials for external
    systems, so this covers all secret-bearing fields, not just llm.api_key:
    - llm.api_key (BYOM provider key), and each llm.failover[].api_key (#222)
    - aws.secret_access_key / aws.session_token (adr/0016)
    - server_url.secret (server-events signing secret)
    - tools[].webhook_secret (custom-tool signing secret)
    - mcp_servers[].headers / .env (documented as carrying Authorization)

    New secret-bearing config fields MUST be added here, and to `map_secrets`.
    """
    if not isinstance(config, dict):
        return config
    out = {**config}

    llm = out.get("llm")
    if isinstance(llm, dict):
        out["llm"] = _mask_llm(llm)

    aws = out.get("aws")
    if isinstance(aws, dict):
        masked = {
            k: (MASK if aws.get(k) is not None else None)
            for k in ("secret_access_key", "session_token")
            if k in aws
        }
        if masked:
            out["aws"] = {**aws, **masked}

    server_url = out.get("server_url")
    if isinstance(server_url, dict) and server_url.get("secret") is not None:
        out["server_url"] = {**server_url, "secret": MASK}

    tools = out.get("tools")
    if isinstance(tools, list):
        out["tools"] = [
            {**t, "webhook_secret": MASK}
            if isinstance(t, dict) and t.get("webhook_secret") is not None
            else t
            for t in tools
        ]

    mcp_servers = out.get("mcp_servers")
    if isinstance(mcp_servers, list):
        out["mcp_servers"] = [_mask_mcp_server(s) for s in mcp_servers]

    return out


def sanitize_target(target: Any) -> Any:
    """Mask the inline config an eval target may carry (#74).

    `{"type": "inline", "agent": {...}}` holds the same secrets an agent row
    does, and unlike `resolved_config` it cannot be stored masked — the runner
    reads it back to resolve the target.
    """
    if not isinstance(target, dict) or not isinstance(target.get("agent"), dict):
        return target
    return {**target, "agent": sanitize_config(target["agent"])}


def _mask_llm(llm: dict[str, Any]) -> dict[str, Any]:
    out = {**llm}
    if out.get("api_key") is not None:
        out["api_key"] = MASK
    if isinstance(out.get("failover"), list):
        out["failover"] = [
            _mask_llm(e) if isinstance(e, dict) else e for e in out["failover"]
        ]
    return out


def _mask_mcp_server(server: Any) -> Any:
    """Mask header/env values (keeping keys, so the shape stays visible)."""
    if not isinstance(server, dict):
        return server
    masked = {**server}
    if isinstance(server.get("headers"), dict) and server["headers"]:
        masked["headers"] = {k: MASK for k in server["headers"]}
    if isinstance(server.get("env"), dict) and server["env"]:
        masked["env"] = {k: MASK for k in server["env"]}
    return masked


def map_secrets(config: Any, fn: Callable[[str], str]) -> Any:
    """`config` with `fn` applied to every secret it carries, as a new dict.

    The paths `sanitize_config` masks, plus aws.access_key_id: the read shows
    that one (it names the key, it does not unlock it), but #338 asks for the
    static AWS keys at rest, and an id stored next to its secret is half the
    pair. Only non-empty strings reach `fn` — a None or "" carries no secret,
    which is what lets a write without one go through with no key set. Never
    mutates `config`; a non-dict is returned as it came.
    """
    if not isinstance(config, dict):
        return config
    out = {**config}

    llm = out.get("llm")
    if isinstance(llm, dict):
        out["llm"] = _map_llm(llm, fn)

    aws = out.get("aws")
    if isinstance(aws, dict):
        out["aws"] = _map_keys(
            aws, ("access_key_id", "secret_access_key", "session_token"), fn
        )

    server_url = out.get("server_url")
    if isinstance(server_url, dict):
        out["server_url"] = _map_keys(server_url, ("secret",), fn)

    tools = out.get("tools")
    if isinstance(tools, list):
        out["tools"] = [
            _map_keys(t, ("webhook_secret",), fn) if isinstance(t, dict) else t
            for t in tools
        ]

    mcp_servers = out.get("mcp_servers")
    if isinstance(mcp_servers, list):
        out["mcp_servers"] = [_map_mcp_server(s, fn) for s in mcp_servers]

    return out


def map_inline_target(target: Any, fn: Callable[[str], str]) -> Any:
    """`map_secrets` over the inline config an eval target may carry.

    The same shape test as `sanitize_target`: whatever holds an `agent` dict is
    an inline target, and anything else (an agent reference) has no secrets.
    """
    if not isinstance(target, dict) or not isinstance(target.get("agent"), dict):
        return target
    return {**target, "agent": map_secrets(target["agent"], fn)}


def map_dynamic_config(metadata: Any, fn: Callable[[str], str]) -> Any:
    """`map_secrets` over the inline agent a call or SMS session keeps.

    Call-init's `dynamic_config` (ADR-0017) is stored under the row's metadata
    unmasked, because the pipeline builds from it; the rest of the metadata is
    the caller's own and carries no config secret, so it passes untouched.
    """
    if not isinstance(metadata, dict) or not isinstance(
        metadata.get("dynamic_config"), dict
    ):
        return metadata
    return {**metadata, "dynamic_config": map_secrets(metadata["dynamic_config"], fn)}


def _map_value(value: Any, fn: Callable[[str], str]) -> Any:
    return fn(value) if isinstance(value, str) and value else value


def _map_keys(
    block: dict[str, Any], keys: tuple[str, ...], fn: Callable[[str], str]
) -> dict[str, Any]:
    return {**block, **{k: _map_value(block[k], fn) for k in keys if k in block}}


def _map_llm(llm: dict[str, Any], fn: Callable[[str], str]) -> dict[str, Any]:
    out = _map_keys(llm, ("api_key",), fn)
    if isinstance(out.get("failover"), list):
        out["failover"] = [
            _map_llm(e, fn) if isinstance(e, dict) else e for e in out["failover"]
        ]
    return out


def _map_mcp_server(server: Any, fn: Callable[[str], str]) -> Any:
    """Map header/env values; the keys are names, not secrets, and stay."""
    if not isinstance(server, dict):
        return server
    out = {**server}
    for field in ("headers", "env"):
        if isinstance(server.get(field), dict):
            out[field] = {k: _map_value(v, fn) for k, v in server[field].items()}
    return out
