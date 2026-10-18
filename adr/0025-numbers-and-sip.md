# ADR-0025: Buying and releasing numbers; SIP through a carrier or Asterisk

**Status:** Accepted (search and buy on Twilio built in #319; Telnyx and
release not yet)
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

## As built

**Search and buy on Twilio** (#319). `GET /v1/phone-numbers/available` and
`POST /v1/phone-numbers/purchase`, both admin-only. The buy writes no binding:
it returns what bind needs (`carrier`, `e164_number`, `external_number_sid`)
and is audited as `phone_number`/`purchased`, keyed by the E.164 number.

Twilio has no project credential (ADR-0020's addendum: a Twilio account rides
on the number), so "the account a project already holds" is, on Twilio, the
`twilio_account_sid` + `twilio_auth_token` pair the request names (headers on
the search, to keep the token out of URLs; the body on the buy), else the
platform's `TWILIO_*` account, exactly as bind resolves it. The buy echoes
`twilio_account_sid` so the number is bound with the same pair. A `twilio`
project credential was the alternative; it would change how webhooks are
verified and was not needed for this.

Search and buy are concrete methods on the carrier adapters that sell numbers,
not `TelephonyAdapter` actions: Asterisk has no number store (ADR-0022).
