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
from WebRTC). A URL already stored, or an inline config from call-init, is
never refused; `warn_if_plain_http` logs each delivery to one that the gate
would now refuse, at most once a minute per kind, naming only the host.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from loguru import logger

from turncall.config.settings import Environment, get_settings
from turncall.services.webhook_verification import RefusalLog, bounded

if TYPE_CHECKING:
    from turncall.config.settings import Settings

# A Kubernetes Service name, judged by the URL's hostname only.
IN_CLUSTER_SUFFIXES = (".svc", ".svc.cluster.local")

# The deliveries `warn_if_plain_http` is told about; the RefusalLog's keys.
DELIVERY_KINDS = frozenset({"webhook", "call_init", "tool", "mcp"})

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


def _parts(url: str) -> tuple[str, str]:
    """The URL's scheme (lowercased) and hostname; empty when unparseable."""
    try:
        parts = urlsplit(url.strip())
        return parts.scheme, parts.hostname or ""
    except ValueError:  # e.g. an unclosed IPv6 bracket
        return "", ""


def is_https_or_in_cluster(url: str) -> bool:
    """What an outbound URL must be where plain http is not allowed: https://,
    or http:// to a cluster-internal Service. The suffix must be the host's,
    not something smuggled into the path or the userinfo."""
    scheme, host = _parts(url)
    if scheme == "https":
        return True
    return scheme == "http" and host.endswith(IN_CLUSTER_SUFFIXES)


def check_outbound_url(
    url: str, *, label: str, settings: Settings | None = None
) -> str:
    """The URL, stripped, when it may be written; otherwise a ValueError that
    names the field, what it needs and the settings that decide it."""
    url = url.strip()
    scheme, _ = _parts(url)
    if scheme not in ("http", "https"):
        got = f", not {bounded(scheme, 16)}://" if scheme else ""
        raise ValueError(f"{label} must be an https:// URL{got}")
    if is_https_or_in_cluster(url):
        return url
    if settings is None:
        settings = get_settings()
    if plain_http_allowed(settings):
        return url
    raise ValueError(f"{label} must use https://: {_refused_because(settings)}")


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
    interval per kind. Returns whether it logged."""
    if kind not in DELIVERY_KINDS:
        raise ValueError(f"unknown delivery kind {kind!r}")
    scheme, host = _parts(url)
    if scheme != "http" or is_https_or_in_cluster(url):
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
