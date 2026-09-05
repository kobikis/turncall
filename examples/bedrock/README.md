# AWS Bedrock / Nova Sonic Example

Run a voice agent on AWS — either **Bedrock** as the LLM leg of a cascade
pipeline, or **Amazon Nova Sonic 2** as a native speech-to-speech model.

Design notes live in [`adr/0016`](../../adr/0016-bedrock-and-nova-sonic.md).

## Quick Start

```bash
# Bedrock LLM (cascade: Deepgram STT → Bedrock → Deepgram TTS)
./examples/bedrock/run.sh

# Nova Sonic 2 (speech-to-speech, no STT/TTS stage at all)
./examples/bedrock/run.sh --mode s2s --voice matthew
```

Then talk to it in the browser with the
[WebRTC client](../webrtc-client), or pass `--twilio-number` /
`--twilio-number-sid` to answer a real phone.

## Credentials

This example never writes AWS keys into the agent config. Credentials are
resolved by the **server**, in this order:

| Source | How |
|---|---|
| `--role-arn` | TurnCall assumes the role per call. Preferred — temporary credentials, no stored secret. |
| Ambient chain | Env vars, an SSO profile, an EC2 instance profile, ECS task role, or EKS IRSA. |
| Static keys | Per-agent, and off by default — see below. |

The quickest check that the server can reach AWS at all:

```bash
aws sts get-caller-identity
```

**SSO is a workstation mechanism.** `aws sso login` is an interactive browser
flow a server cannot perform, and the cached token expires. It's fine for local
development; in production use an instance profile, IRSA, or `--role-arn`.

Static per-agent keys exist as an escape hatch and are rejected at agent-create
unless `AWS_AGENT_CREDENTIALS_ENABLED=true`, because they persist in
`config_blob`, which is unencrypted JSONB.

## Region and model access

Two things bite here, both region-shaped:

1. **`AWS_REGION` also points at the S3 bucket.** Pass `--region` explicitly —
   Bedrock model availability rarely matches where your bucket lives.
2. **Model access is opt-in per model, per region.** Grant it in the Bedrock
   console first, or calls fail with `AccessDeniedException`.

Model ids pass through verbatim, in any of the three forms AWS accepts:

```
anthropic.claude-3-5-sonnet-20241022-v2:0     direct
us.anthropic.claude-3-5-sonnet-20241022-v2:0  cross-region inference profile
arn:aws:bedrock:...:provisioned-model/...     provisioned throughput
```

## Nova Sonic notes

- Nova Sonic **2** (`amazon.nova-2-sonic-v1:0`) is the default.
- Voices: `matthew`, `tiffany`, `amy`, `lupe`, `carlos`.
- `--endpointing-sensitivity LOW|MEDIUM|HIGH` tunes how quickly it decides the
  caller stopped speaking. **Nova Sonic 2 only** — it is ignored on the older
  `amazon.nova-sonic-v1:0`.
- Sessions expire about every 6 minutes and roll over transparently. TurnCall
  re-resolves AWS credentials on each rollover, so calls longer than a
  credential's lifetime keep working.

## Passing model-specific parameters

`llm.extra` forwards to Bedrock's `additionalModelRequestFields` — this is how
Anthropic extended thinking is reached:

```json
"llm": {
  "provider": "bedrock",
  "model": "anthropic.claude-3-7-sonnet-20250219-v1:0",
  "extra": {"thinking": {"type": "enabled", "budget_tokens": 1024}}
}
```

`llm.reasoning_effort` stays OpenAI-family only (see `adr/0014`).
