"""The provider values each role accepts, and the account each one bills to.

One source for the agent config validators and for `GET /v1/providers`, so
the two cannot drift (#163).

A role provider is what an agent's config names (`openai_live`, `bedrock`); an
**account** is what a [[Project credential]] is keyed by (ADR-0020). Deepgram is
one account serving STT and TTS, OpenAI one account serving four roles, and AWS
one account behind both `bedrock` and the `aws` S2S provider.
"""

STT_PROVIDERS = ("deepgram", "openai", "elevenlabs", "cartesia")
LLM_PROVIDERS = (
    "openai",
    "anthropic",
    "openrouter",
    "bedrock",
    "ollama",
    "custom_openai",
)
TTS_PROVIDERS = ("deepgram", "openai", "elevenlabs", "cartesia")
S2S_PROVIDERS = ("openai", "openai_live", "google", "aws")
AVATAR_PROVIDERS = ("heygen", "tavus")

ROLE_PROVIDERS: dict[str, tuple[str, ...]] = {
    "stt": STT_PROVIDERS,
    "llm": LLM_PROVIDERS,
    "tts": TTS_PROVIDERS,
    "s2s": S2S_PROVIDERS,
    "avatar": AVATAR_PROVIDERS,
}

AWS_ACCOUNT = "aws"

# Role providers whose account is spelled differently. `ollama` and
# `custom_openai` have none: the key, if any, belongs to whatever endpoint the
# agent's own base_url names, so only `llm.api_key` can carry it.
_ACCOUNT_ALIASES = {"openai_live": "openai", "bedrock": AWS_ACCOUNT}
_NO_ACCOUNT = frozenset({"ollama", "custom_openai"})


def account_for(provider: str) -> str | None:
    """The credential account a role provider runs on, or None for none."""
    if provider in _NO_ACCOUNT:
        return None
    return _ACCOUNT_ALIASES.get(provider, provider)


def accounts() -> dict[str, dict[str, list[str]]]:
    """Every credential account -> {role: [role providers it serves]}."""
    out: dict[str, dict[str, list[str]]] = {}
    for role, providers in ROLE_PROVIDERS.items():
        for provider in providers:
            account = account_for(provider)
            if account is not None:
                out.setdefault(account, {}).setdefault(role, []).append(provider)
    return out


CREDENTIAL_ACCOUNTS = frozenset(accounts())
