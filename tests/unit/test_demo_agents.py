"""The demo's agents file: refused at boot when a call could not run it.

A demo call has no database, project or carrier behind it, so an agent that
needs one fails when the server starts — naming the agent and the field —
never on a visitor's call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from turncall.demo.agents import DemoAgentError, load_demo_agents

pytestmark = pytest.mark.unit

AGENT: dict[str, Any] = {
    "system_prompt": "You are the demo agent.",
    "first_message": "Hello.",
    "max_call_duration_seconds": 600,
    "tools": [
        {
            "name": "end_call",
            "description": "End the call after saying goodbye.",
            "parameters_schema": {},
        }
    ],
}


def _file(tmp_path: Path, agents: Any) -> Path:
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(agents))
    return path


def test_agents_load_by_name_in_order(tmp_path) -> None:
    agents = load_demo_agents(
        _file(tmp_path, {"b": AGENT, "a": AGENT}), max_call_seconds=180
    )
    assert list(agents) == ["b", "a"]
    assert [t.name for t in agents["b"].tools] == ["end_call"]


def test_a_call_is_capped_at_the_demos_ceiling(tmp_path) -> None:
    agents = load_demo_agents(_file(tmp_path, {"a": AGENT}), max_call_seconds=120)
    assert agents["a"].max_call_duration_seconds == 120


def test_a_shorter_call_keeps_its_own_limit(tmp_path) -> None:
    short = {**AGENT, "max_call_duration_seconds": 60}
    agents = load_demo_agents(_file(tmp_path, {"a": short}), max_call_seconds=120)
    assert agents["a"].max_call_duration_seconds == 60


@pytest.mark.parametrize(
    ("change", "named"),
    [
        (
            {
                "tools": [
                    {
                        "name": "transfer_call",
                        "description": "d",
                        "parameters_schema": {},
                    }
                ]
            },
            "transfer_call",
        ),
        (
            {
                "tools": [
                    {
                        "name": "book",
                        "description": "d",
                        "parameters_schema": {},
                        "webhook_url": "https://example.com/book",
                    }
                ]
            },
            "book",
        ),
        (
            {"mcp_servers": [{"name": "crm", "url": "https://mcp.example.com"}]},
            "MCP servers",
        ),
        ({"ambience": {"sound": "00000000-0000-0000-0000-000000000001"}}, "ambience"),
        ({"avatar": {"enabled": True, "provider": "heygen"}}, "video avatar"),
    ],
)
def test_what_needs_the_platform_is_refused(tmp_path, change, named) -> None:
    with pytest.raises(DemoAgentError, match=named) as raised:
        load_demo_agents(
            _file(tmp_path, {"front-desk": {**AGENT, **change}}),
            max_call_seconds=180,
        )
    assert "'front-desk'" in str(raised.value)


def test_an_unknown_field_is_an_error_not_a_setting_ignored(tmp_path) -> None:
    with pytest.raises(DemoAgentError, match="'a' is invalid"):
        load_demo_agents(
            _file(tmp_path, {"a": {**AGENT, "sytem_prompt": "typo"}}),
            max_call_seconds=180,
        )


@pytest.mark.parametrize("content", [{}, [], "not json"])
def test_a_file_with_no_agents_is_refused(tmp_path, content) -> None:
    path = tmp_path / "agents.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    with pytest.raises(DemoAgentError):
        load_demo_agents(path, max_call_seconds=180)


def test_a_missing_file_is_refused(tmp_path) -> None:
    with pytest.raises(DemoAgentError, match="cannot read"):
        load_demo_agents(tmp_path / "absent.json", max_call_seconds=180)
