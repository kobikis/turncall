"""Credential gating for the live suite.

A missing key is a skip, never a failure: these run on a developer's machine
with a .env, and in CI with none.
"""

import os

import pytest
from dotenv import load_dotenv

load_dotenv()


def require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} is not set")
    return value


@pytest.fixture
def openai_key() -> str:
    return require("OPENAI_API_KEY")


@pytest.fixture
def anthropic_key() -> str:
    return require("ANTHROPIC_API_KEY")


@pytest.fixture
def google_key() -> str:
    return require("GOOGLE_API_KEY")


@pytest.fixture
def typesafe_key() -> str:
    """Jev, the eval judge's classifier (#141). Also needs the `evals-jev`
    extra, which the base install deliberately does not carry."""
    key = require("TYPESAFE_API_KEY")
    pytest.importorskip(
        "pipecat.classifiers.jev.classifier",
        reason="the 'evals-jev' extra is not installed",
    )
    return key


@pytest.fixture
def deepgram_key() -> str:
    return require("DEEPGRAM_API_KEY")


@pytest.fixture
def elevenlabs_key() -> str:
    return require("ELEVENLABS_API_KEY")


@pytest.fixture
def assemblyai_key() -> str:
    return require("ASSEMBLYAI_API_KEY")


@pytest.fixture
def telnyx_key() -> str:
    return require("TELNYX_API_KEY")
