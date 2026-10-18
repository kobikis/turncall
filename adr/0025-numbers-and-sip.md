# ADR-0025: Buying and releasing numbers; SIP through a carrier or Asterisk

**Status:** Accepted (built: search and buy on Twilio in #319 and Telnyx in
#320, release on both in #321)
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

**Search and buy on Telnyx** (#320). The same endpoints with `carrier=telnyx`,
on the project's Telnyx account (its `telnyx` credential, else the platform's
`TELNYX_API_KEY`), as bind resolves it. A Telnyx purchase is a number order
that completes asynchronously, and neither the order's id nor its line's is
the phone number's id that bind stores and release deletes. The buy re-reads
the order for about five seconds; once it succeeds it finds the number on the
account by E.164 and answers `active` with that id. An order still pending
(Telnyx is slow, or the country needs documents) answers `pending` with no id
and the order's `telnyx_order_id`. That needs no new state: bind already looks
a Telnyx number up by E.164, so it binds the number once Telnyx has put it on
the account and refuses it until then. Holding the request open until the
order finishes was rejected (an order awaiting documents can take days), as was
a stored "ordered" row (bind has no unbound state to keep it in). Once the
order is placed only a `failure` status is an error: the money is spent, so a
pending purchase is audited like a finished one.

**Release** (#321). `POST /v1/phone-numbers/release`, admin-only, on Twilio
and Telnyx, on the account a purchase uses. A binding has no unbound state
(`phone_numbers.routing_target_type` is required), so a number is bound
exactly when TurnCall has a `phone_numbers` row for it, and a release names
the number on the carrier account by its E.164 rather than by a binding id.
"Refuses a number that is still bound" is a `409` when any project has a row
for that E.164; the caller unbinds first, which leaves the number on the
account (unbind is unchanged and never releases). "An explicit confirmation
field" is `confirm`, which must repeat `e164_number` exactly: a boolean
`confirm: true` was rejected as too easy to send by default or copy between
requests, and repeating the number is what a person confirming would type.
Anything else is a `422` before TurnCall looks the number up.

The number is found on the account by E.164 (Twilio's PN SID, Telnyx's phone
number id; a number the account does not hold, including a Telnyx number
still on order, is a `422`) and deleted with a concrete `release_number` on
each adapter, again not a `TelephonyAdapter` action. The release is audited
as `phone_number`/`released`, keyed by the E.164 number with the released
number as its "before", only once the carrier has released it: a refused
release raises, so nothing commits.

**The shared platform account** (#321). The platform's carrier accounts are
shared: Twilio's `TWILIO_*` account (a request with no account pair) and
Telnyx's `TELNYX_API_KEY` (a project with no `telnyx` credential) hold every
project's numbers, and the operator's own. Admin-only is not enough there: an
admin of one project could release another's number. So a release that would
run on the platform's account is allowed only for a number this project
bought through `POST /v1/phone-numbers/purchase`; anything else is a `403`
(`forbidden`, `details.account: "platform"`), refused before the carrier is
asked anything, so the refusal does not say whether the account holds the
number. `403` rather than `422`: the request is well formed and names a real
number; this caller is not allowed to act on it, as with a role refusal. A
release on the project's own account (its Twilio pair, its `telnyx`
credential) is unrestricted apart from the `409` and `422` rules: that account
is the operator's.

Purchases are recorded in a `purchased_numbers` table (migration
`c9e3a5f7b1d4`): project, carrier, E.164, and which account (`platform` or
`own`), written by every purchase (a pending Telnyx order too) and deleted by
the release; the project's FK cascade takes its rows with it. The audit log
was rejected as the source: its entries can be dropped by retention, and it
answers "what happened", not "whose is it now". `phone_numbers` was rejected
too: it is the binding, and a release needs the number unbound. A platform
purchase drops any other project's `platform` row for the same number (the
number was for sale, so that row is stale: it left the account outside
TurnCall), and a partial unique index keeps one `platform` row per carrier
and number. A number bought before this table existed, or put on the platform
account by hand, can be released there only by the operator, on the carrier's
console.
