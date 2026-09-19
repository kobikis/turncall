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
