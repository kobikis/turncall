"""The provider values each role accepts, and the account each one bills to.

One source for the agent config validators and for `GET /v1/providers`, so
the two cannot drift (#163).

A role provider is what an agent's config names (`openai_live`, `bedrock`); an
**account** is what a [[Project credential]] is keyed by (ADR-0020). Deepgram is
one account serving STT and TTS, OpenAI one account serving four roles, Google one
account behind both its LLM and Gemini Live, and AWS one account behind both
`bedrock` and the `aws` S2S provider.

A [[carrier]] is listed as the `carrier` role: Telnyx's and Asterisk's accounts
are project credentials (ADR-0022), Twilio's is not — it is per number or the platform's
TWILIO_* (`services/twilio_accounts.py`).
"""

from turncall.domain.carriers import CARRIERS, TWILIO

STT_PROVIDERS = ("deepgram", "openai", "elevenlabs", "cartesia", "assemblyai", "xai")
LLM_PROVIDERS = (
    "openai",
    "anthropic",
    "openrouter",
    "bedrock",
    "google",
    "ollama",
    "custom_openai",
)
TTS_PROVIDERS = ("deepgram", "openai", "elevenlabs", "cartesia")
S2S_PROVIDERS = ("openai", "openai_live", "google", "aws", "xai")
AVATAR_PROVIDERS = ("heygen", "tavus")
# What grades an eval run (EVAL_JUDGE_PROVIDER or a scenario's judge block).
# Listed so a project can hold the key it runs on; `ollama` is local and needs
# none. Not an agent config role — no agent validator reads it.
JUDGE_PROVIDERS = ("openai", "anthropic", "jev")

ROLE_PROVIDERS: dict[str, tuple[str, ...]] = {
    "stt": STT_PROVIDERS,
    "llm": LLM_PROVIDERS,
    "tts": TTS_PROVIDERS,
    "s2s": S2S_PROVIDERS,
    "avatar": AVATAR_PROVIDERS,
    "judge": JUDGE_PROVIDERS,
    "carrier": CARRIERS,
}

AWS_ACCOUNT = "aws"

# Role providers whose account is spelled differently. `ollama` and
# `custom_openai` have none: the key, if any, belongs to whatever endpoint the
# agent's own base_url names, so only `llm.api_key` can carry it.
_ACCOUNT_ALIASES = {"openai_live": "openai", "bedrock": AWS_ACCOUNT, "jev": "typesafe"}
# Twilio has no project credential: its account rides on the number.
_NO_ACCOUNT = frozenset({"ollama", "custom_openai", TWILIO})


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
