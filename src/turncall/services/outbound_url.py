"""Outbound URLs that carry call data must be https:// (#340).

Webhook subscriptions, call-init (`server_url`), tool webhooks and MCP servers
are sent transcripts, caller numbers and tool arguments. One rule says where a
plain http:// one is allowed: ALLOW_PLAIN_HTTP_URLS when it is set, otherwise
development only, as for unverifiable carrier webhooks (#337), so staging and
production refuse it. `TURNCALL_ENV` defaults to development, so a deployment
that never set it allows them, and startup says so in plain words.

A host ending in .svc or .svc.cluster.local is a Kubernetes Service: the
request never leaves the cluster, and in-cluster traffic is plain HTTP unless
a mesh adds TLS (the builder's Agent Backends answer there). It is allowed
everywhere. Loopback and private addresses are not: in production they are far
more likely a misconfiguration or a probe than a receiver.

`check_outbound_url` is the write-time gate (a 422 from the API schemas, a 400
from WebRTC). A URL already stored, or an inline config (a call-init answer,
an eval run's `inline` target, a `turncall-demo` agents file), is never
refused for plain http; `warn_if_plain_http` logs each delivery to one that
the gate would now refuse, at most once a minute per kind, naming only the
host, and `warn_stored_plain_http` names the stored ones at startup.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from urllib.parse import SplitResult, urlsplit

from loguru import logger

from turncall.config.settings import Environment, get_settings
from turncall.services.webhook_verification import RefusalLog, bounded

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from turncall.config.settings import Settings

# A Kubernetes Service name, judged by the URL's hostname only.
IN_CLUSTER_SUFFIXES = (".svc", ".svc.cluster.local")

# The deliveries `warn_if_plain_http` is told about; the RefusalLog's keys.
# `call_init` is the request that asks for an agent; `server_event` is every
# other event sent to a server_url, so each has its own allowance.
DELIVERY_KINDS = frozenset({"webhook", "call_init", "server_event", "tool", "mcp"})

# One for the process, keyed by delivery kind (never by URL or host, which
# tenants choose and are unbounded).
plain_http_deliveries = RefusalLog(
    note="more plain http deliveries of this kind since the last"
)


def plain_http_allowed(settings: Settings | None = None) -> bool:
    """Whether a plain http:// outbound URL is allowed: ALLOW_PLAIN_HTTP_URLS
    when it is set, otherwise only in development."""
    if settings is None:
        settings = get_settings()
    if settings.allow_plain_http_urls is not None:
        return settings.allow_plain_http_urls
    return settings.environment == Environment.DEVELOPMENT


def _split(url: str) -> SplitResult | None:
    """The URL's parts, or None when it does not parse (an unclosed IPv6
    bracket, say)."""
    try:
        return urlsplit(url.strip())
    except ValueError:
        return None


def _parts(url: str) -> tuple[str, str]:
    """The URL's scheme (lowercased) and hostname; empty when unparseable."""
    parts = _split(url)
    if parts is None:
        return "", ""
    return parts.scheme, parts.hostname or ""


def _allowed_anywhere(scheme: str, host: str) -> bool:
    """https://, or http:// to a host that is a cluster-internal Service."""
    if scheme == "https":
        return True
    return scheme == "http" and host.endswith(IN_CLUSTER_SUFFIXES)


def is_https_or_in_cluster(url: str) -> bool:
    """What an outbound URL must be where plain http is not allowed: https://,
    or http:// to a cluster-internal Service. The suffix must be the host's,
    not something smuggled into the path or the userinfo."""
    return _allowed_anywhere(*_parts(url))


def check_outbound_url(
    url: str,
    *,
    label: str,
    settings: Settings | None = None,
    allow_plain_http: bool = False,
) -> str:
    """The URL, stripped, when it may be written; otherwise a ValueError that
    names the field, what it needs and the settings that decide it.

    `allow_plain_http` takes plain http whatever the settings say, for an
    inline config, which is never refused for it; the URL must still be an
    http(s) one with a host."""
    url = url.strip()
    parts = _split(url)
    if parts is None:
        raise ValueError(f"{label} is not a URL")
    scheme, host = parts.scheme, parts.hostname or ""
    if scheme in ("http", "https"):
        if not host:
            raise ValueError(
                f"{label} must name a host, as in {scheme}://receiver.example.com/hook"
            )
        if allow_plain_http or _allowed_anywhere(scheme, host):
            return url
        if settings is None:
            settings = get_settings()
        if plain_http_allowed(settings):
            return url
        raise ValueError(f"{label} must use https://: {_refused_because(settings)}")
    http_too = allow_plain_http or plain_http_allowed(settings)
    accepted = "an http:// or https:// URL" if http_too else "an https:// URL"
    if scheme and url.lower().startswith(f"{scheme}://"):
        raise ValueError(f"{label} must be {accepted}, not {bounded(scheme, 16)}://")
    # `localhost:8080/hook` parses with "localhost" as its scheme.
    start = "https:// or http://" if http_too else "https://"
    raise ValueError(
        f"{label} must be {accepted}: it has no scheme, so start it with {start}"
    )


def _refused_because(settings: Settings) -> str:
    """Why plain http is refused here, and how to allow it, for a 422."""
    if settings.allow_plain_http_urls is False:
        return (
            "ALLOW_PLAIN_HTTP_URLS=false refuses plain http:// whatever "
            "TURNCALL_ENV is, except to an in-cluster Service (a host ending "
            "in .svc or .svc.cluster.local)"
        )
    return (
        f"plain http:// is refused on a TURNCALL_ENV={settings.environment.value} "
        "deployment, except to an in-cluster Service (a host ending in .svc "
        "or .svc.cluster.local); ALLOW_PLAIN_HTTP_URLS=true allows it"
    )


def warn_if_plain_http(
    kind: str, url: str, *, settings: Settings | None = None, **fields: object
) -> bool:
    """Before a delivery: warn when it goes to a plain http:// URL that
    `check_outbound_url` would now refuse. The delivery still happens. Logs
    the host only (a URL's path or query can hold a token), at most once per
    interval per kind. Returns whether it logged.

    Never raises: a log line must never cost the delivery, so whatever goes
    wrong here, an unknown kind included, is itself only logged."""
    try:
        if kind not in DELIVERY_KINDS:
            logger.warning("plain_http_warning_unknown_kind", kind=bounded(kind, 32))
            return False
        scheme, host = _parts(url)
        if scheme != "http" or _allowed_anywhere(scheme, host):
            return False
        if plain_http_allowed(settings):
            return False
        return plain_http_deliveries.warn(
            kind,
            "plain_http_delivery | a {kind} delivery to plain http:// host {host} "
            "is sent unencrypted; writing this URL now would be refused, so "
            "change it to https://",
            kind=kind,
            host=host,
            **fields,
        )
    except Exception as exc:
        logger.warning("plain_http_warning_failed", error=type(exc).__name__)
        return False


# Startup names at most this many (project, kind) groups, each with at most
# this many hosts, and reads at most this many rows from each table, so a
# large deployment's report stays a few lines and a few queries.
_STORED_GROUPS_LOGGED = 50
_STORED_HOSTS_SHOWN = 5
_STORED_ROWS_READ = 10_000


def _agent_urls(blob: Any) -> Iterator[tuple[str, object]]:
    """An agent config's outbound URLs, by delivery kind."""
    if not isinstance(blob, dict):
        return
    for tool in blob.get("tools") or []:
        if isinstance(tool, dict):
            yield "tool", tool.get("webhook_url")
    for server in blob.get("mcp_servers") or []:
        if isinstance(server, dict):
            yield "mcp", server.get("url")
    server_url = blob.get("server_url")
    if isinstance(server_url, dict):
        yield "server_event", server_url.get("url")


async def warn_stored_plain_http(
    session: AsyncSession, settings: Settings
) -> dict[tuple[str, str], list[str]]:
    """At startup, name the stored plain http:// URLs that writing now would
    refuse: webhook subscriptions, numbers' call-init `server_url`, and
    agents' tool, MCP and `server_url` URLs. They are still delivered to.

    One warning per project and kind, with a count and the hosts, never a
    path or query (they can hold a token). Only where plain http is not
    allowed. Returns the hosts found, by (project id, kind)."""
    if plain_http_allowed(settings):
        return {}
    from sqlalchemy import Text, cast, select

    from turncall.storage.models import (
        AgentRow,
        PhoneNumberRow,
        WebhookSubscriptionRow,
    )

    found: dict[tuple[str, str], list[str]] = defaultdict(list)

    def note(project_id: object, kind: str, url: object) -> None:
        if not isinstance(url, str):
            return
        scheme, host = _parts(url)
        if scheme == "http" and not _allowed_anywhere(scheme, host):
            found[(str(project_id), kind)].append(host)

    hooks = await session.execute(
        select(WebhookSubscriptionRow.project_id, WebhookSubscriptionRow.url)
        .where(WebhookSubscriptionRow.url.ilike("%http://%"))
        .limit(_STORED_ROWS_READ)
    )
    for project_id, url in hooks:
        note(project_id, "webhook", url)
    numbers = await session.execute(
        select(PhoneNumberRow.project_id, PhoneNumberRow.server_url)
        .where(PhoneNumberRow.server_url.ilike("%http://%"))
        .limit(_STORED_ROWS_READ)
    )
    for project_id, url in numbers:
        note(project_id, "call_init", url)
    # A JSON string that starts http:// is `"http://` in the blob's text; the
    # walk then keeps only the URL fields.
    agents = await session.execute(
        select(AgentRow.project_id, AgentRow.config_blob)
        .where(cast(AgentRow.config_blob, Text).ilike('%"http://%'))
        .limit(_STORED_ROWS_READ)
    )
    for project_id, blob in agents:
        for kind, url in _agent_urls(blob):
            note(project_id, kind, url)

    groups = sorted(found.items())
    for (project_id, kind), hosts in groups[:_STORED_GROUPS_LOGGED]:
        distinct = sorted(set(hosts))
        shown = ", ".join(bounded(h) for h in distinct[:_STORED_HOSTS_SHOWN])
        if len(distinct) > _STORED_HOSTS_SHOWN:
            shown += f" and {len(distinct) - _STORED_HOSTS_SHOWN} more"
        logger.warning(
            "plain_http_stored | project {project_id} has {count} stored plain "
            "http:// {kind} URL(s), to {hosts}, that writing now would refuse; "
            "deliveries to them still go out and log plain_http_delivery, so "
            "change them to https://",
            project_id=project_id,
            kind=kind,
            count=len(hosts),
            hosts=shown,
        )
    if len(groups) > _STORED_GROUPS_LOGGED:
        logger.warning(
            "plain_http_stored | {more} more project and kind groups hold "
            "stored plain http:// URLs",
            more=len(groups) - _STORED_GROUPS_LOGGED,
        )
    return dict(found)


def log_plain_http_policy(settings: Settings) -> str:
    """At startup, say whether plain http:// outbound URLs are allowed. When
    they are allowed because TURNCALL_ENV was never set, it says so. Allowed
    anywhere but an explicit development deployment is a warning. Returns the
    message."""
    subject = "plain_http_urls | plain http:// webhook, call-init, tool and MCP URLs"
    setting = settings.allow_plain_http_urls
    if setting is None and settings.environment_is_default:
        message = (
            f"{subject} are allowed, because TURNCALL_ENV is not set and "
            "defaults to development; set TURNCALL_ENV=production (or staging) "
            "on any deployment that is not a development one, and they are "
            "refused"
        )
        logger.warning(message)
        return message
    why = (
        f"ALLOW_PLAIN_HTTP_URLS={str(setting).lower()}"
        if setting is not None
        else f"TURNCALL_ENV={settings.environment.value}"
    )
    if not plain_http_allowed(settings):
        message = (
            f"{subject} are refused ({why}); https:// or an in-cluster *.svc "
            "host is required"
        )
        logger.info(message)
    elif settings.environment == Environment.DEVELOPMENT:
        message = f"{subject} are allowed ({why})"
        logger.info(message)
    else:
        message = f"{subject} are allowed ({why}) on a {settings.environment.value} deployment"
        logger.warning(message)
    return message
