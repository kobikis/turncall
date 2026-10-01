# Project credentials: provider keys in their own encrypted table

The Builder console is gaining a page where a workspace connects its own provider
accounts — its Deepgram key, its ElevenLabs key — so calls run on that
workspace's account rather than the operator's. Those keys have to reach the
pipeline, which runs here. This records where they are stored, and why not in the
obvious place.

## What is added

- **`project_credentials`** — one row per `(project_id, provider)` holding an
  encrypted JSON blob. A single-key provider stores `{"api_key": "..."}`; the AWS
  providers store the shape `AWSConfig` already validates (`access_key_id`,
  `secret_access_key`, `session_token`, `role_arn`, `external_id`, `profile`,
  `region`). One credential per provider **account**, never per role: Deepgram is
  one key serving STT and TTS, OpenAI one key serving STT, LLM, TTS and S2S.
- **`GET /v1/providers`** — the valid provider values per role, and for each
  provider two booleans: whether a platform key is configured, and whether this
  project holds a credential. Presence only; no value is ever returned.
- Resolution in `pipeline_factory` for every provider that takes a key, including
  the four that read `os.environ` directly today (Deepgram, ElevenLabs, HeyGen,
  Tavus).

## Why not an `api_key` field on the STT/TTS/S2S config blocks

That is the obvious design and it is one line of precedent away: `llm.api_key`
already exists and works. It was rejected because this codebase has already
written down why not.

`AWS_AGENT_CREDENTIALS_ENABLED` defaults to **false**, and `domain/models.py`
gives the reason: *"they land in config_blob, which is unencrypted JSONB."* An
`api_key` on `STTConfig` lands in the same column, holding the same class of
secret, with no flag guarding it — while a feature whose entire purpose is to
collect provider keys through a web form would become the largest supplier that
column has ever had.

It is also worse per key than the AWS case it would mirror. The console's tenant
is a workspace and its credential is workspace-scoped, so one key becomes one row
per agent in that workspace. Masking (`sanitize_config`, #91) keeps the value out
of API responses; it does nothing about the value in Postgres, in a backup, in a
replica, or in a query.

A separate table costs a migration and a resolver, and buys encryption at rest,
absence from every agent read, and a `sanitize_config` that does not have to grow
each time a provider is added.

## Precedence: narrowest wins

```
agent config (llm.api_key, config.aws)
  -> project credential
    -> platform environment
```

The same ordering the eval [[judge]] already uses — a scenario's block, then the
platform's, then pipecat's default. An agent carrying its own inline key keeps
it, deliberately: one agent can be pinned to a different account without
disconnecting the workspace's.

The `openai` LLM provider is fixed in the same change. It passes the platform key
unconditionally today and discards `config.llm.api_key` — harmless while nothing
sets one, and a lie the moment a console card reports that provider as connected.

## What `AWS_AGENT_CREDENTIALS_ENABLED` still governs

The inline path only. A [[Project credential]] is encrypted and outside the
config surface, so the flag's stated reason does not apply to it and it does not
gate it. `AWS_AGENT_CREDENTIALS_ENABLED=false` therefore no longer means "no
static AWS keys here" — it means "no static AWS keys *in agent config* here".
Read as the former it is a security flag misunderstood in the unsafe direction,
which is why this is stated rather than left to be inferred.

## Encryption

A new `CREDENTIAL_ENCRYPTION_KEY`, Fernet, matching what the builder repo already
does for GitHub tokens and the Pipedream client secret. It fails closed: unset
while credentials exist is a startup error, never a silent fall back to
plaintext. Like `API_KEY_HASH_SECRET` it is set once and not rotated — rotating
orphans every stored credential and every workspace has to reconnect.

## What is deferred

The Builder mints one TurnCall project per *agent*
(`create_project(f"builder-{slug}")`), so a workspace-scoped credential is
written once per agent and a rotation is N writes. That is a misalignment on the
Builder's side — TurnCall's project is its tenant boundary and the Builder spends
it on "agent" — and correcting it (one project per workspace) collapses N to 1
with no change here. Recorded so the fan-out is read as a consequence of that
mapping rather than as this design's shape.

## Status

Accepted; implemented (#161-#164). The API is `PUT/GET/DELETE /v1/credentials`
and `GET /v1/providers`; the resolver is `services/credentials.py`
(`Credentials`), built per call in `pipeline_builder.load_call_credentials`.
The account namespace lives in `domain/providers.py`: `openai_live` bills to
`openai`, `bedrock` and the `aws` S2S provider share `aws`, and `ollama` /
`custom_openai` have no account — their key, if any, belongs to the endpoint
their own `base_url` names. Work outside a call — text turns, post-call
analysis, takeaways, the transfer briefing, KB embeddings, the eval judge —
resolves in the same order through `credentials.for_project`, so the platform
environment is a fallback everywhere and a requirement nowhere.

Telephony, listed below as staying platform-level, was later given a narrower
answer: a **phone number** may carry its own Twilio account
(`services/twilio_accounts.py`), sealed with the same key. Per number rather
than per project because Twilio's own unit is the number — webhooks name their
account by `AccountSid`, and one project can hold numbers from two accounts.
