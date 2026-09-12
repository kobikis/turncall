"""Guards on the dependency bounds that carry a reason.

Two upgrades this week arrived silently through unbounded pins — mcp 2.x
renamed the fields mcp_client reads, and openai 3 moves the HTTP stack under
every Pipecat OpenAI service. Neither showed up in a test, because the suite
never opens a socket. These assert the shape of what's installed, so a resolve
that drifts past a bound fails here rather than in a call.
"""

import importlib.metadata as md

import pytest


def _version(dist: str) -> tuple[int, ...]:
    return tuple(int(p) for p in md.version(dist).split(".")[:2] if p.isdigit())


@pytest.mark.unit
def test_pipecat_is_at_least_1_10() -> None:
    """1.10 is the floor for the MCP extra's 1.x/2.x support and for the
    WebSocket audio-pacing fix."""
    assert _version("pipecat-ai") >= (1, 10)


@pytest.mark.unit
def test_the_mcp_extra_is_installed() -> None:
    """pipecat-ai[mcp] — MCPClient raises ImportError without it, and the
    extra is easy to drop by editing the long extras list."""
    import pipecat.services.mcp_service  # noqa: F401


@pytest.mark.unit
def test_openai_sdk_is_within_the_declared_bound() -> None:
    """pyproject allows <4 deliberately. Anything at or past 4 is a major the
    Pipecat services haven't been checked against."""
    major = _version("openai")[0]
    assert 1 <= major < 4


@pytest.mark.unit
def test_mcp_exposes_the_field_names_mcp_client_reads() -> None:
    """mcp 2.x renamed Tool.inputSchema -> input_schema and
    CallToolResult.isError -> is_error. Both are read by services/mcp_client
    and both failures are swallowed, so an unpinned resolve produced an image
    where every MCP server quietly returned no tools."""
    from mcp.types import CallToolResult, Tool

    assert "inputSchema" in Tool.model_fields
    assert "isError" in CallToolResult.model_fields


@pytest.mark.unit
def test_anthropic_sdk_major_is_one_of_the_two_pipecat_supports() -> None:
    """pipecat 1.10 widened anthropic to <2. The 1.x line moved temperature,
    top_k and top_p out of the Messages API parameters and into extra_body,
    and requires an explicit region on a supplied Bedrock client — which
    aws_credentials.bedrock_kwargs() passes. Anything past 2 is unchecked."""
    assert md.version("anthropic").split(".")[0] in {"0", "1"}
