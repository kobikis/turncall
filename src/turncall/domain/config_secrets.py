"""Masking of the secrets an agent config carries, for anything on its way out.

Lives in `domain/` rather than next to the agent response schema because it has
more than one exit to guard: the agent endpoints, an eval run's snapshot, and
the eval webhook payloads (#91). A copy per exit is how the eval paths shipped
without one.
"""

from typing import Any

MASK = "***"


def sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Mask every secret in an agent config before returning it in API responses.

    A read-only key must not be able to exfiltrate credentials for external
    systems, so this covers all secret-bearing fields, not just llm.api_key:
    - llm.api_key (BYOM provider key)
    - aws.secret_access_key / aws.session_token (adr/0016)
    - server_url.secret (server-events signing secret)
    - tools[].webhook_secret (custom-tool signing secret)
    - mcp_servers[].headers / .env (documented as carrying Authorization)

    New secret-bearing config fields MUST be added here.
    """
    if not isinstance(config, dict):
        return config
    out = {**config}

    llm = out.get("llm")
    if isinstance(llm, dict) and llm.get("api_key") is not None:
        out["llm"] = {**llm, "api_key": MASK}

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
