# ADR-0025: Buying and releasing numbers; SIP through a carrier or Asterisk

**Status:** Accepted (not yet built)
**Date:** 2026-10-16
**Relates to:** ADR-0020 (project credentials), ADR-0022 (carrier boundary).
Tracked in #307.

## Context

TurnCall answers calls on Twilio, Telnyx and an operator's Asterisk over ARI. A
number must already exist on the operator's carrier account before it can be
bound, and there is no generic SIP trunk. Every managed platform sells numbers
in its own product and accepts a SIP trunk; LiveKit sells US numbers at
$1/month.

The buyers we build for next are developers embedding voice and regulated
self-hosters. Neither is a contact center choosing between Genesys and Five9.

## Decision

**No SIP stack in the engine.** A native SIP/RTP endpoint is a large build for a
small part of our audience. A SIP trunk reaches TurnCall in one of two ways, both
documented as first-class paths:

- into the operator's Asterisk, which TurnCall already drives over ARI;
- into Telnyx or Twilio elastic SIP trunking, landing on a number that carrier
  already routes to TurnCall.

**Search, buy and release numbers through the engine API**, on the Twilio and
Telnyx accounts a project already holds (ADR-0020). Buying spends the operator's
money and releasing is irreversible (the number goes back to the carrier), so:

- both are admin-only (`ProjectRole.ADMIN`);
- release refuses a number that is still bound;
- release requires an explicit confirmation field in the request body.

A bought number is bound with the same call as any other.

**New carriers wait for demand.** Pipecat's Plivo, Exotel and Vonage serializers
keep that cheap when it comes.

**Contact-center connectors are out of scope** (Genesys AudioHook, Amazon
Connect, Five9). They serve a buyer we are not targeting.

## Considered options

- **A native SIP endpoint.** The right answer for a contact-center product; for
  ours, Asterisk and carrier trunking cover the cases at a fraction of the work.
- **Number purchase in the builder only.** The builder never calls a carrier
  (it delegates binding to TurnCall); keeping purchase in the engine keeps that
  true and gives API users the same ability.
