"""The harness's two seams still exist (#214).

The end-to-end harness test needs Postgres and skips without it; this does
not. A renamed provider factory or adapter constructor would otherwise send
every harness test to real providers and a real carrier, or skip silently.
"""

import pytest

from tests.harness import FakeCarrier, Script, install_stub_providers
from tests.harness.stubs import StubLLM, StubSTT, StubTTS

pytestmark = pytest.mark.unit


def test_stubs_replace_every_provider_factory(monkeypatch):
    from turncall.orchestrator import pipeline_factory

    install_stub_providers(Script(), monkeypatch.setattr)

    assert isinstance(pipeline_factory._create_stt_service(None, None), StubSTT)
    assert isinstance(pipeline_factory._create_llm_service(None, None), StubLLM)
    assert isinstance(pipeline_factory._create_tts_service(None, None), StubTTS)


async def test_every_twilio_account_reaches_the_fake_carrier(monkeypatch):
    from turncall.services.twilio_accounts import TwilioAccount

    carrier = FakeCarrier()
    carrier.install(monkeypatch.setattr)

    await TwilioAccount("ACown", "token", shared=False).adapter().end_call("CA1")

    assert [(a.kind, a.account_sid, a.params) for a in carrier.actions] == [
        ("hang_up", "ACown", {"call_sid": "CA1"})
    ]
