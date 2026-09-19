"""C7 — Nova Sonic under aws-sdk-bedrock-runtime 0.9 / smithy 0.8.

Pipecat 1.11 raised the `aws-nova-sonic` extra to
`aws_sdk_bedrock_runtime>=0.6.0,<0.10`, which pulled the whole smithy stack up
with it (`smithy-core` 0.3 → 0.8, plus `smithy-http`, `smithy-aws-core`,
`smithy-aws-event-stream`, `awscrt`). Those libraries are the signing and
event-stream framing underneath every Nova Sonic call, and no hermetic test
touches either — the unit suite constructs the service with fake credentials
and never opens a socket.

`create_client()` and `open_stream()` are the service's own public methods, so
this exercises the same path a real S2S call takes rather than a parallel one.
"""

import os

import pytest
from loguru import logger

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


def _require_aws() -> None:
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        pytest.skip("AWS_ACCESS_KEY_ID is not set")


def _skip_if_no_model_access(exc: Exception) -> None:
    """Nova Sonic needs its own model access, granted separately from Claude's
    (see C5). A denial is not an upgrade regression, so it skips rather than
    fails."""
    text = str(exc)
    for blocked in (
        "not available for this account",
        "use case details",
        "AccessDenied",
        "don't have access",
    ):
        if blocked in text:
            pytest.skip(f"Nova Sonic model access not granted: {text[:140]}")


def _service():
    from turncall.domain.models import AgentConfig, S2SConfig
    from turncall.orchestrator.s2s_config import create_s2s_service

    config = AgentConfig(
        name="nova-sonic-probe",
        system_prompt="You are terse.",
        pipeline_mode="s2s",
        s2s=S2SConfig(provider="aws"),
    )
    return create_s2s_service(config, openai_api_key="")


async def test_the_bedrock_runtime_sdk_is_in_pipecats_window() -> None:
    """Pipecat 1.11 declares `>=0.6.0,<0.10`. Which version resolved decides
    whether the rest of this file tests what we think it does."""
    import importlib.metadata as md

    version = md.version("aws-sdk-bedrock-runtime")
    major, minor = (int(part) for part in version.split(".")[:2])

    assert (major, minor) >= (0, 6), (
        f"aws-sdk-bedrock-runtime {version} is below pipecat's floor"
    )
    assert (major, minor) < (0, 10), (
        f"aws-sdk-bedrock-runtime {version} is above pipecat's ceiling"
    )


async def test_the_service_builds_a_client_on_the_new_smithy_stack() -> None:
    """`create_client()` assembles a smithy `Config` with a credentials
    resolver and a SigV4 auth scheme — the three objects the 0.3 → 0.8 jump
    could have reshaped. No socket; this is the construction seam.
    """
    _require_aws()

    client = _service().create_client()

    assert client is not None
    assert type(client).__name__.endswith("BedrockRuntimeClient")


async def _first_output(model: str, *, timeout: float):
    """Open a stream for `model` and return what Bedrock says first.

    `open_stream()` is lazy — it hands back a `DuplexEventStream` in ~0ms
    without touching the network, and does so even for a model that does not
    exist. Awaiting output is what actually signs the request and reads a
    frame back, so that is where a broken smithy stack would show.
    """
    import asyncio

    from turncall.domain.models import AgentConfig, S2SConfig
    from turncall.orchestrator.s2s_config import create_s2s_service

    config = AgentConfig(
        name="nova-sonic-probe",
        system_prompt="You are terse.",
        pipeline_mode="s2s",
        s2s=S2SConfig(provider="aws", model=model),
    )
    service = create_s2s_service(config, openai_api_key="")
    stream = await service.open_stream(service.create_client())
    try:
        return await asyncio.wait_for(stream.await_output(), timeout=timeout)
    finally:
        try:
            await stream.input_stream.close()
        except Exception as exc:  # awscrt is noisy closing a cancelled stream
            logger.debug("nova sonic probe: closing the stream raised {}", exc)


async def test_bedrock_rejects_a_model_that_does_not_exist() -> None:
    """The control. Proves the probe below is reaching Bedrock at all: a
    signed round trip comes back with a validation error naming the model. If
    SigV4 or the event-stream framing were broken, this would fail as an auth
    or transport error instead."""
    import asyncio

    _require_aws()

    with pytest.raises(Exception) as caught:
        await _first_output("amazon.this-model-does-not-exist-v9:0", timeout=20)

    assert not isinstance(caught.value, asyncio.TimeoutError), (
        "a nonexistent model produced no answer from Bedrock — the request "
        "never arrived, so this file proves nothing about the transport"
    )
    _skip_if_no_model_access(caught.value)
    assert "model identifier is invalid" in str(caught.value).lower(), caught.value


async def test_nova_sonic_accepts_the_stream() -> None:
    """The real model, same path. Bedrock has nothing to say until pipecat
    sends its session events, so blocking is the pass: the stream was accepted
    and no validation, signing or framing error came back."""
    import asyncio

    _require_aws()

    try:
        with pytest.raises(asyncio.TimeoutError):
            await _first_output("amazon.nova-2-sonic-v1:0", timeout=6)
    except Exception as exc:  # anything other than the expected silence
        _skip_if_no_model_access(exc)
        raise


async def test_the_model_is_nova_sonic_not_the_openai_sentinel() -> None:
    """`S2SConfig.model` defaults to `gpt-realtime-2.1` for every provider;
    `_create_nova_sonic` swaps that sentinel for its own model. The google path
    has no such swap (see C6), so this pins the behaviour that is correct."""
    service = _service()

    assert "nova" in str(service._settings.model).lower(), service._settings.model
