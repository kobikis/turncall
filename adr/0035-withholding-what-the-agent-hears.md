# ADR-0035: Withholding — keeping a caller's card number from the agent

**Status:** Accepted (not yet built)
**Date:** 2026-11-25
**Amends:** ADR-0024 ("redact what leaves the call, never what the model
hears"), for the entity types an agent opts into. Follows ADR-0034 (pipecat
1.17), whose STT `text_transforms` make it possible.

## Context

ADR-0024 keeps redaction away from the live call on purpose: an agent that
cannot see the caller's phone number cannot book the appointment. That holds for
most PII. It does not hold for a card number or an SSN read out on a call: the
agent has no use for one that a keypad or a transfer could not serve better, and
every place it reaches — the LLM vendor, the context, tool arguments, the stored
transcript — is a place a PCI or privacy review has to account for. "The LLM
vendor never saw the card number" is a claim a platform can only make if the
value is gone before the first inference.

pipecat 1.17 rewrites each transcript in the STT service, before it is pushed,
so the context, observers and tracing see only the rewritten text.

## Decision

**An agent may withhold entity types from itself**: `stt.withhold:
["CREDIT_CARD", "US_SSN"]`, off by default. Each caller transcript is rewritten
in the STT stage, so the value never reaches the context, the LLM vendor, a
tool's arguments, a transcript event or anything stored. A withheld value reads
`<ENTITY_TYPE>`, as a redacted one does.

- **Regular expressions in process, not Presidio.** Card numbers are checked
  with Luhn. Presidio runs in a worker process (ADR-0004, ADR-0024); waiting on
  it inside every turn is latency on the call path, and the withheld types have
  a fixed shape.
- **A number read in chunks is still withheld.** Callers read cards in groups
  and the STT finalizes them separately, so a final transcript of digits only is
  held until the run of digits ends, then withheld as a whole. The cost is a
  short wait only while someone is reading digits; nothing reaches the LLM
  before the check.
- **The agent is told.** The build adds one generated line to the prompt: what
  is withheld, never to ask the caller to repeat it, and to offer keypad entry
  or a transfer instead.
- **Cascade only.** An S2S agent sends the caller's audio to its vendor itself,
  so the promise cannot hold there: `stt.withhold` on an S2S agent is refused,
  at the API (422) and by agent acceptance for a call-init inline agent.

## Consequences

- A tool can no longer receive a withheld value: taking a card payment by voice
  is not possible on an agent that withholds `CREDIT_CARD`. That is the point;
  payment goes through DTMF capture or a transfer.
- The stored transcript of a withholding agent shows the placeholder even for a
  project without redaction, and the call recording still holds the audio —
  recording retention (ADR-0024) remains the guidance for audio.
