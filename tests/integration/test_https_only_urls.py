"""Outbound URLs are https:// outside development, through the running app (#340).

Booted as development, as every harness test is, then switched to production:
a plain http:// URL for each kind (webhook subscription, number call-init,
agent server_url / tool / MCP, a registered tool) is a 422 that leaves nothing
behind; https:// and an in-cluster Service are written; back in development
the same plain http:// URL is taken, as it always was.
"""

from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, run_app
from tests.harness.stubs import Script
from turncall.config.settings import Environment, get_settings

pytestmark = pytest.mark.integration

PLAIN = "http://receiver.example.com/hook"
TLS = "https://receiver.example.com/hook"
IN_CLUSTER = "http://turncall-agent-9001.agents.svc.cluster.local:8000/events"


def _agent(url: str) -> dict[str, Any]:
    return {
        **HARNESS_AGENT,
        "server_url": {"url": url},
        "tools": [{"name": "book", "description": "Book", "webhook_url": url}],
        "mcp_servers": [{"name": "crm", "transport": "http", "url": url}],
    }


def _refusal_locs(response) -> list[tuple]:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_error"
    errors = body["details"]["errors"]
    assert all("must use https://" in e["msg"] for e in errors), errors
    return sorted(tuple(e["loc"]) for e in errors)


async def test_plain_http_urls_are_refused_in_production(monkeypatch) -> None:
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        api = app.api
        agent_id = await app.create_agent(HARNESS_AGENT)
        e164 = "+1555" + "8675309"
        number = app.carrier.add_number(e164)
        bind = {
            "e164_number": e164,
            "external_number_sid": number.sid,
            "routing_target_type": "webhook",
        }
        hooks_before = (await api.get("/v1/webhooks")).json()["data"]

        monkeypatch.setattr(get_settings(), "environment", Environment.PRODUCTION)
        refused = {
            "webhook": await api.post("/v1/webhooks", json={"url": PLAIN}),
            "bind": await api.post(
                "/v1/phone-numbers", json={**bind, "server_url": PLAIN}
            ),
            "agent": await api.post(
                "/v1/agents", json={"name": "plain", "config": _agent(PLAIN)}
            ),
            "agent_put": await api.put(
                f"/v1/agents/{agent_id}", json={"config": _agent(PLAIN)}
            ),
            "tool": await api.post(
                "/v1/tools",
                json={"name": "book", "description": "Book", "webhook_url": PLAIN},
            ),
        }
        hooks_after_refusal = (await api.get("/v1/webhooks")).json()["data"]
        numbers_after_refusal = (await api.get("/v1/phone-numbers")).json()["data"]

        written = {
            "webhook": await api.post("/v1/webhooks", json={"url": IN_CLUSTER}),
            "bind": await api.post(
                "/v1/phone-numbers", json={**bind, "server_url": IN_CLUSTER}
            ),
            "agent": await api.post(
                "/v1/agents", json={"name": "tls", "config": _agent(TLS)}
            ),
            "agent_put": await api.put(
                f"/v1/agents/{agent_id}", json={"config": _agent(IN_CLUSTER)}
            ),
        }
        number_id = written["bind"].json()["data"]["id"]
        update = {"routing_target_type": "webhook"}
        refused["update"] = await api.put(
            f"/v1/phone-numbers/{number_id}", json={**update, "server_url": PLAIN}
        )
        written["update"] = await api.put(
            f"/v1/phone-numbers/{number_id}", json={**update, "server_url": TLS}
        )

        monkeypatch.setattr(get_settings(), "environment", Environment.DEVELOPMENT)
        taken = await api.post("/v1/webhooks", json={"url": PLAIN})

    assert _refusal_locs(refused["webhook"]) == [("body", "url")]
    assert _refusal_locs(refused["bind"]) == [("body", "server_url")]
    assert _refusal_locs(refused["update"]) == [("body", "server_url")]
    config_locs = [
        ("body", "config", "mcp_servers", 0, "url"),
        ("body", "config", "server_url", "url"),
        ("body", "config", "tools", 0, "webhook_url"),
    ]
    assert _refusal_locs(refused["agent"]) == config_locs
    assert _refusal_locs(refused["agent_put"]) == config_locs
    assert _refusal_locs(refused["tool"]) == [("body", "webhook_url")]
    # A refusal writes nothing.
    assert hooks_after_refusal == hooks_before
    assert numbers_after_refusal == []

    for name, response in written.items():
        assert response.status_code in (200, 201), (name, response.text)
    assert written["webhook"].json()["data"]["url"] == IN_CLUSTER
    assert written["update"].json()["data"]["server_url"] == TLS
    assert taken.status_code == 201, taken.text


async def test_startup_names_the_stored_plain_http_urls(monkeypatch) -> None:
    """Rows written while plain http was allowed are named at startup once it
    is not: per project and kind, with a count and the hosts only."""
    from loguru import logger

    from turncall.services.outbound_url import warn_stored_plain_http
    from turncall.storage.database import get_session_factory

    secret_url = "http://receiver.example.com/s3cret-path?token=s3cret"
    async with run_app(
        Script(), set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        api = app.api
        e164 = "+1555" + "8675310"
        number = app.carrier.add_number(e164)
        written = [
            await api.post("/v1/webhooks", json={"url": secret_url}),
            await api.post("/v1/webhooks", json={"url": PLAIN}),
            await api.post("/v1/webhooks", json={"url": IN_CLUSTER}),
            await api.post("/v1/webhooks", json={"url": TLS}),
            await api.post(
                "/v1/phone-numbers",
                json={
                    "e164_number": e164,
                    "external_number_sid": number.sid,
                    "routing_target_type": "webhook",
                    "server_url": PLAIN,
                },
            ),
            await api.post(
                "/v1/agents", json={"name": "stored-plain", "config": _agent(PLAIN)}
            ),
            await api.post(
                "/v1/agents", json={"name": "stored-tls", "config": _agent(TLS)}
            ),
        ]
        for response in written:
            assert response.status_code in (200, 201), response.text

        settings = get_settings()
        async with get_session_factory()() as session:
            allowed = await warn_stored_plain_http(session, settings)

        monkeypatch.setattr(settings, "environment", Environment.PRODUCTION)
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
        try:
            async with get_session_factory()() as session:
                found = await warn_stored_plain_http(session, settings)
        finally:
            logger.remove(sink)

    assert allowed == {}
    # The harness's own event sink is a plain http:// subscription too.
    host = "receiver.example.com"
    ours = {
        kind: [h for h in hosts if h == host]
        for (project_id, kind), hosts in found.items()
        if project_id == app.project_id
    }
    assert ours == {
        "webhook": [host, host],
        "call_init": [host],
        "tool": [host],
        "mcp": [host],
        "server_event": [host],
    }
    # Hosts only: never a path or a query, which can hold a token.
    assert not any("s3cret" in line for line in lines)
