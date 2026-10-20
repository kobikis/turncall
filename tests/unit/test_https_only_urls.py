"""Outbound URLs are https:// outside development, checked when written (#340).

Every API field that takes a URL TurnCall later sends call data to: a webhook
subscription's `url`, a number's call-init `server_url` (bind and update), and
in an agent's config `server_url.url`, a tool's `webhook_url` and an MCP
server's `url`, plus `POST /v1/tools`. Plain http is refused in staging and
production, allowed in development (the default) or with
ALLOW_PLAIN_HTTP_URLS=true, and allowed everywhere to an in-cluster Service.
Inline configs (an eval run's `inline` target, a demo agents file) are not
refused for it.
"""

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ValidationError

from turncall.api.deps import get_session
from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.api.v1.schemas.evals import EvalTarget
from turncall.api.v1.schemas.phone_numbers import (
    BindPhoneNumberRequest,
    UpdatePhoneNumberRequest,
)
from turncall.api.v1.schemas.tools import RegisterToolRequest
from turncall.api.v1.schemas.webhooks import CreateWebhookRequest
from turncall.auth.context import AuthContext
from turncall.auth.dependencies import resolve_auth_context
from turncall.config.settings import get_settings
from turncall.domain.enums import ProjectRole

pytestmark = pytest.mark.unit

PLAIN = "http://receiver.example.com/hook"


def _tool(url: str) -> dict[str, Any]:
    return {"name": "book", "description": "Book a table", "webhook_url": url}


def _mcp(url: str) -> dict[str, Any]:
    return {"name": "crm", "transport": "http", "url": url}


# Each field, as the body that carries it is parsed, and how to read the URL
# back off the result.
FIELDS: dict[str, tuple[Callable[[str], BaseModel], Callable[[Any], str]]] = {
    "webhook url": (lambda u: CreateWebhookRequest(url=u), lambda m: m.url),
    "number server_url (bind)": (
        lambda u: BindPhoneNumberRequest(
            e164_number="+15550001111",
            external_number_sid="PN" + "0" * 32,
            routing_target_type="webhook",
            server_url=u,
        ),
        lambda m: m.server_url,
    ),
    "number server_url (update)": (
        lambda u: UpdatePhoneNumberRequest(routing_target_type="webhook", server_url=u),
        lambda m: m.server_url,
    ),
    "agent server_url.url": (
        lambda u: AgentConfigSchema.model_validate({"server_url": {"url": u}}),
        lambda m: m.server_url.url,
    ),
    "agent tool webhook_url": (
        lambda u: AgentConfigSchema.model_validate({"tools": [_tool(u)]}),
        lambda m: m.tools[0].webhook_url,
    ),
    "registered tool webhook_url": (
        lambda u: RegisterToolRequest(**_tool(u)),
        lambda m: m.webhook_url,
    ),
    "agent MCP server url": (
        lambda u: AgentConfigSchema.model_validate({"mcp_servers": [_mcp(u)]}),
        lambda m: m.mcp_servers[0].url,
    ),
}


@pytest.fixture
def env(monkeypatch) -> Iterator[Callable[..., None]]:
    """Set TURNCALL_ENV (None = unset) and ALLOW_PLAIN_HTTP_URLS (None =
    unset) for the settings the validators read."""

    def _set(environment: str | None = "production", allow: str | None = None) -> None:
        for name, value in (
            ("TURNCALL_ENV", environment),
            ("ALLOW_PLAIN_HTTP_URLS", allow),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


@pytest.mark.parametrize("environment", ["production", "staging"])
@pytest.mark.parametrize("field", FIELDS)
def test_plain_http_is_refused_outside_development(env, field, environment) -> None:
    env(environment)
    build, _ = FIELDS[field]

    with pytest.raises(ValidationError) as caught:
        build(PLAIN)

    message = str(caught.value)
    assert "must use https://" in message
    assert f"TURNCALL_ENV={environment}" in message
    assert "ALLOW_PLAIN_HTTP_URLS=true" in message


@pytest.mark.parametrize(
    "url",
    [
        "http://turncall-agent-9001.agents.svc.cluster.local:8000/events",
        "http://backend.agents.svc/events",
    ],
)
@pytest.mark.parametrize("field", FIELDS)
def test_an_in_cluster_service_may_be_plain_http(env, field, url) -> None:
    env("production")
    build, read = FIELDS[field]
    assert read(build(url)) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example.com/x.svc",
        "http://backend.svc@evil.example.com/hook",
        "http://127.0.0.1:9000/hook",
        "http://10.0.0.5/hook",
    ],
)
def test_only_the_host_is_exempt_and_loopback_is_not(env, url) -> None:
    env("production")
    with pytest.raises(ValidationError, match="must use https://"):
        CreateWebhookRequest(url=url)


@pytest.mark.parametrize("field", FIELDS)
def test_https_is_accepted_and_stripped(env, field) -> None:
    env("production")
    build, read = FIELDS[field]
    assert read(build("  https://receiver.example.com/hook ")) == (
        "https://receiver.example.com/hook"
    )


@pytest.mark.parametrize("field", FIELDS)
def test_the_opt_in_allows_plain_http_in_production(env, field) -> None:
    env("production", allow="true")
    build, read = FIELDS[field]
    assert read(build(PLAIN)) == PLAIN


@pytest.mark.parametrize("environment", [None, "development"])
@pytest.mark.parametrize("field", FIELDS)
def test_development_allows_plain_http(env, field, environment) -> None:
    # Unset is development: what every test and local run relies on.
    env(environment)
    build, read = FIELDS[field]
    assert read(build(PLAIN)) == PLAIN


@pytest.mark.parametrize("field", FIELDS)
def test_opting_out_refuses_plain_http_in_development(env, field) -> None:
    env("development", allow="false")
    build, _ = FIELDS[field]
    with pytest.raises(ValidationError, match="ALLOW_PLAIN_HTTP_URLS=false"):
        build(PLAIN)


@pytest.mark.parametrize("field", FIELDS)
def test_a_scheme_other_than_http_is_refused_everywhere(env, field) -> None:
    # A webhook subscription used to take any scheme at all.
    env(None, allow="true")
    build, _ = FIELDS[field]
    # Where plain http is allowed, the message says so.
    with pytest.raises(
        ValidationError, match="must be an http:// or https:// URL, not ftp://"
    ):
        build("ftp://receiver.example.com/hook")


def test_an_absent_optional_url_is_not_checked(env) -> None:
    env("production")
    config = AgentConfigSchema.model_validate(
        {"tools": [{"name": "end_call", "description": "End"}]}
    )
    assert config.server_url.url is None
    assert config.tools[0].webhook_url is None
    assert RegisterToolRequest(name="x", description="d").webhook_url is None


def _inline_agent(url: str) -> dict[str, Any]:
    return {
        "server_url": {"url": url},
        "tools": [_tool(url)],
        "mcp_servers": [_mcp(url)],
    }


def test_an_eval_inline_target_is_not_refused_for_plain_http(env) -> None:
    # An inline config is never refused for it: a mocked tool is never called.
    env("production")
    target = EvalTarget(type="inline", agent=_inline_agent(PLAIN))
    assert target.agent is not None

    # It is still a URL check: another scheme is refused, as for an agent.
    with pytest.raises(ValidationError, match="must be an http:// or https:// URL"):
        EvalTarget(type="inline", agent={"tools": [_tool("ftp://receiver.example")]})
    # And the same config, stored, is refused.
    with pytest.raises(ValidationError, match="must use https://"):
        AgentConfigSchema.model_validate(_inline_agent(PLAIN))


def test_a_demo_agents_file_is_not_refused_for_plain_http(env, tmp_path) -> None:
    from turncall.demo.agents import load_demo_agents

    env("production")
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"front-desk": {"server_url": {"url": PLAIN}}}))

    agents = load_demo_agents(path, max_call_seconds=60)

    assert agents["front-desk"].server_url.url == PLAIN


# Through the routes: each refusal is a 422 in the API's error shape, raised
# before the route touches the database.

H = {"Authorization": "Bearer x"}
NUMBER_ID = uuid.uuid4()
AGENT_ID = uuid.uuid4()

ROUTES: dict[str, tuple[str, str, dict[str, Any], tuple[str, ...]]] = {
    "webhook url": (
        "POST",
        "/v1/webhooks",
        {"url": PLAIN},
        ("body", "url"),
    ),
    "number server_url (bind)": (
        "POST",
        "/v1/phone-numbers",
        {
            "e164_number": "+15550001111",
            "external_number_sid": "PN" + "0" * 32,
            "routing_target_type": "webhook",
            "server_url": PLAIN,
        },
        ("body", "server_url"),
    ),
    "number server_url (update)": (
        "PUT",
        f"/v1/phone-numbers/{NUMBER_ID}",
        {"routing_target_type": "webhook", "server_url": PLAIN},
        ("body", "server_url"),
    ),
    "agent server_url.url (create)": (
        "POST",
        "/v1/agents",
        {"name": "a", "config": {"server_url": {"url": PLAIN}}},
        ("body", "config", "server_url", "url"),
    ),
    "agent tool webhook_url (replace)": (
        "PUT",
        f"/v1/agents/{AGENT_ID}",
        {"config": {"tools": [_tool(PLAIN)]}},
        ("body", "config", "tools", 0, "webhook_url"),
    ),
    "agent MCP server url (create)": (
        "POST",
        "/v1/agents",
        {"name": "a", "config": {"mcp_servers": [_mcp(PLAIN)]}},
        ("body", "config", "mcp_servers", 0, "url"),
    ),
    "registered tool webhook_url": (
        "POST",
        "/v1/tools",
        _tool(PLAIN),
        ("body", "webhook_url"),
    ),
}


@pytest.fixture
def admin(app) -> None:
    ctx = AuthContext(
        project_id=uuid.uuid4(), api_key_id=uuid.uuid4(), role=ProjectRole.ADMIN
    )
    # A session that fails the test if a route gets as far as using it.
    session = AsyncMock(execute=AsyncMock(side_effect=AssertionError("db touched")))
    session.add = MagicMock(side_effect=AssertionError("db touched"))
    app.app.dependency_overrides[resolve_auth_context] = lambda: ctx
    app.app.dependency_overrides[get_session] = lambda: session


@pytest.mark.parametrize("route", ROUTES)
def test_each_route_refuses_plain_http_with_a_422(app, admin, env, route) -> None:
    env("production")
    method, path, body, loc = ROUTES[route]

    r = app.request(method, path, headers=H, json=body)

    assert r.status_code == 422, r.text
    payload = r.json()
    assert payload["code"] == "validation_error"
    assert payload["error"] == "Validation error"
    errors = payload["details"]["errors"]
    assert [tuple(e["loc"]) for e in errors] == [loc]
    assert "must use https://" in errors[0]["msg"]


def test_an_eval_runs_inline_target_is_not_a_422_for_plain_http(
    app, admin, env, monkeypatch
) -> None:
    from turncall.api.errors import NotFoundError
    from turncall.api.v1 import evals

    env("production")
    # Past validation, the route looks its scenario up: a 404 here.
    monkeypatch.setattr(
        evals,
        "_scenarios_to_run",
        AsyncMock(side_effect=NotFoundError("Scenario", "x")),
    )
    r = app.post(
        "/v1/eval-runs",
        headers=H,
        json={
            "scenario_id": str(uuid.uuid4()),
            "target": {"type": "inline", "agent": _inline_agent(PLAIN)},
        },
    )
    assert r.status_code == 404, r.text
