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
def test_pipecat_is_at_least_1_12() -> None:
    """1.12 is the floor for the classifier-based EvalJudge and
    VoicemailDetector, and for `Frame.interruptible`, which the handoff relies
    on to survive a barge-in. It also forces mcp 2.x — see the pin below."""
    assert _version("pipecat-ai") >= (1, 12)


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
def test_the_mcp_sdk_is_the_2x_line_mcp_client_now_assumes() -> None:
    """mcp_client used to read both spellings — `inputSchema`/`input_schema`
    and `isError`/`is_error` — because both SDK lines were reachable. pipecat
    1.12's mcp extra requires mcp>=2.1.1, so 1.x no longer is, and those reads
    were deleted rather than left as code that cannot run.

    On 2.x the old names are pydantic *aliases*, not attributes, so a resolve
    that somehow dropped back to 1.x would raise AttributeError per tool —
    which connect_servers logs and swallows, quietly returning no tools at
    all. Assert the field names directly."""
    from mcp.types import CallToolResult, Tool

    assert "input_schema" in Tool.model_fields
    assert "is_error" in CallToolResult.model_fields


@pytest.mark.unit
def test_anthropic_sdk_major_is_one_of_the_two_pipecat_supports() -> None:
    """pipecat 1.10 widened anthropic to <2. The 1.x line moved temperature,
    top_k and top_p out of the Messages API parameters and into extra_body,
    and requires an explicit region on a supplied Bedrock client — which
    aws_credentials.bedrock_kwargs() passes. Anything past 2 is unchecked."""
    assert md.version("anthropic").split(".")[0] in {"0", "1"}
