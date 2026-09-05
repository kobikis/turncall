# AWS Bedrock and Nova Sonic: credentials resolved in-process

Adding two AWS providers — `bedrock` for text LLM and `aws` for speech-to-speech
(Nova Sonic 2) — forces a credential design that differs from every other
provider. This records why TurnCall resolves AWS credentials itself instead of
letting boto3 do it, which is the surprising part.

## What is being added

- **`bedrock`** — an LLM [[provider]] on voice and on chat/SMS, via Pipecat's
  `AWSBedrockLLMService` and a `bedrock-runtime.converse()` call respectively.
- **`aws`** — an S2S provider running Nova Sonic 2 (`amazon.nova-2-sonic-v1:0`),
  via Pipecat's `AWSNovaSonicLLMService`.

Polly TTS and Transcribe STT ship in the same Pipecat module and are
deliberately **not** included. They are separate AWS services, not Bedrock, and
widening the STT/TTS provider list is its own decision.

## Why not let boto3 resolve credentials

The obvious design is to pass nothing and let boto3's default chain find
credentials — env vars, an SSO profile, an instance profile, IRSA. That works
for `AWSBedrockLLMService`, whose credential arguments are all
`str | None = None`.

It does not work for Nova Sonic. `AWSNovaSonicLLMService.__init__` requires
`access_key_id: str`, `secret_access_key: str` and `region: str` — non-optional,
with no fallback. The two services also name the same arguments differently
(`aws_access_key` vs `access_key_id`).

So deferring to boto3 would mean one agent config behaving differently by
pipeline mode: an agent relying on an instance profile would work in cascade and
hard-fail the moment someone set `pipeline_mode: "s2s"`. That is the
configuration-silently-diverging failure this codebase has been bitten by
before (ADR-0014).

Instead there is one resolver. It builds a `boto3.Session` from whatever source
applies and calls `get_frozen_credentials()`, producing an explicit
`(access_key_id, secret_access_key, session_token, region)` tuple handed to both
services. SSO, instance profiles, IRSA, ECS task roles and assume-role all
collapse into that one path, and Pipecat's inconsistent argument names stay an
internal detail rather than leaking into agent config.

**SSO is a workstation mechanism, and the docs must say so.** `aws sso login` is
an interactive browser flow; a server cannot perform it. What works server-side
is a cached profile someone already logged into (and which expires), so the
production answer is an instance profile, IRSA, or assume-role.

## Where credentials live

A single top-level `aws` block on the agent config, referenced by both `llm` and
`s2s` — not one block per role. An agent uses one AWS account; duplicating the
credentials invites drift, and drift here means the LLM and the voice leg
authenticating as different principals, which is invisible through the API
because both are masked.

Three sources, in precedence order:

1. **Per-agent `role_arn`**, assumed from platform credentials. The default
   multi-tenant path: it yields temporary credentials and stores no durable
   secret.
2. **Per-agent static keys**, behind `AWS_AGENT_CREDENTIALS_ENABLED`
   (default off, mirroring `BYOM_ENABLED`). Rejected at agent create/update with
   a 400 when disabled — never at call time, when a customer is on the line.
3. **Platform credentials** — env or the host's own role.

The flag exists because `config_blob` is plain JSONB. Secrets are masked on API
read but **not encrypted at rest**, and a long-lived AWS secret key is a
materially worse thing to leak than a vendor API key, because it can reach
services that have nothing to do with voice. Operators should opt into that
deliberately.

## Credential lifetime

Freezing a credential tuple has a ceiling: assume-role and IRSA default to about
an hour. Nova Sonic sessions expire at ~6 minutes and Pipecat rolls over to a new
one (`SessionContinuationParams.transition_threshold_seconds = 360.0`), so a long
call re-establishes its session repeatedly — ordinary phone traffic exercises
this, not just edge cases.

Credentials are therefore **re-resolved on each rollover** rather than captured
once at pipeline build. The rollover is already an integration point; re-resolving
there is cheap and removes the expiry ceiling entirely. Without it, a call
outliving its credentials dies mid-conversation and the caller hears the line go
quiet.

## Smaller decisions

- **Region is explicit**, defaulting to `AWS_REGION` but overridable. `AWS_REGION`
  currently points at the S3 artifact bucket, and Bedrock model availability is
  region-specific — silently sharing one value means moving a bucket changes which
  model answers calls.
- **Model ids pass through verbatim** — direct, `us.`-prefixed cross-region
  inference profiles, and provisioned-throughput ARNs. No allowlist.
- **No validation at agent create.** Checking the model/region pair would put a
  live AWS call and a credential requirement on the create path, breaking agent
  creation before credentials are wired up. Instead the call-time failure is
  mapped explicitly, and the message names the model *and* the region.
- **No `endpoint_url` override** in this iteration. If PrivateLink support is
  added it goes through the BYOM allowlist, like `s2s.base_url`.
- **`llm.extra` forwards to `additionalModelRequestFields`**, which is how
  Anthropic extended thinking is reached on Bedrock. `reasoning_effort` stays
  OpenAI-family-only per ADR-0014 rather than growing a second spelling.
- **`base_url` is rejected** for both AWS providers rather than silently ignored.

## Consequences

- **Two implementations, not one.** `services/llm_text.py` is a separate
  dispatch from the voice pipeline — raw HTTP to OpenAI-compatible endpoints with
  Anthropic special-cased — so chat/SMS needs its own boto3 `converse()` path.
  Shipping voice only would mean the API advertising Bedrock while the text path
  quietly ignored it, so if it is staged, the text path must **reject** `bedrock`
  with a clear error in the interim.
- **`pipecat-ai[aws]` joins the extras** in `pyproject.toml`. boto3 is already
  present transitively via `aioboto3`.
- **The mask list grows.** `schemas/agents.py` must mask the new secret-bearing
  fields alongside `llm.api_key`.
- **`provider` no longer means "vendor".** Every other provider names one;
  `bedrock` names a gateway hosting other vendors' models, so
  `provider: "bedrock", model: "anthropic.claude-..."` names two companies, and
  the same model is reachable through either `anthropic` or `bedrock` with
  different credentials. Recorded in `CONTEXT.md`.
- **The test that matters** asserts the resolved credentials and region actually
  arrive at both services, for each source. That is the direct descendant of the
  hardcoded-STT-config bug: configuration silently ignored is worse than
  configuration that errors.

## Status

Accepted; not yet implemented.
