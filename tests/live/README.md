# Live tests

The unit suite never opens a socket, which is what makes it fast and hermetic —
and also why a dependency can move underneath the whole platform without a
single test going red. The openai 3 upgrade moved the HTTP stack beneath every
OpenAI service and nothing failed; the mcp 2 rename made every MCP server return
zero tools and nothing failed either; the pipecat 1.11 upgrade moved
google-genai across a major version and the whole smithy stack under Nova
Sonic, and nothing failed either.

These tests exist for exactly those cases. They talk to real providers, cost
real money (fractions of a cent), and are **skipped unless the relevant
credential is set**, so `make test` and CI are unaffected.

```bash
uv run pytest -m live            # everything you have keys for
uv run pytest -m live -rs        # ...and why the rest skipped
```

Each file names the gap it closes, from the open-items list:

| File | Gap |
|---|---|
| `test_live_openai_stack.py` | OpenAI STT/TTS and Realtime S2S under the openai 3 SDK |
| `test_live_anthropic.py` | a real Anthropic completion since the temperature fix |
| `test_live_mcp_third_party.py` | MCP against a real external server, not our fixture |
| `test_live_bedrock.py` | Claude on Bedrock — blocked on account model access |
| `test_live_gemini_live.py` | Gemini Live S2S under google-genai 2.x |
| `test_live_nova_sonic.py` | Nova Sonic S2S under aws-sdk-bedrock-runtime 0.9 / smithy 0.8 |

They are not a substitute for a real phone call. Nothing here carries audio over
a carrier, so transport-level faults still need a human with a handset.
