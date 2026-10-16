# ADR-0026: Browser session tokens

**Status:** Accepted (not yet built)
**Date:** 2026-10-16
**Relates to:** ADR-0020 (project credentials); `services/media_token.py`.
Tracked in #308.

## Context

`POST /v1/webrtc/connect` takes a project API key. A browser cannot hold one:
anything shipped to a page is public. So every integrator today writes a relay
that holds the key and forwards the SDP offer and ICE candidates, which is what
the turncall.io demo does. That relay is the main thing standing between
TurnCall and a drop-in client SDK.

## Decision

**The customer's server mints a session token; the browser connects with it.**
`POST /v1/webrtc/sessions`, called with a project API key, returns a token the
browser presents to `/v1/webrtc/connect` instead of a key.

A token is:

- **one call:** it opens a single call and is spent;
- **short-lived:** five minutes to connect;
- **scoped:** one agent in one project;
- **sealed:** the template variables, metadata and knowledge context are fixed by
  the server that minted it. The browser cannot add or change them, so a visitor
  cannot set `{{account_id}}` to somebody else's account.

**No publishable keys.** A per-agent public key limited by origin would let a
widget work with no server at all, but an Origin header is easy to forge, and
every call spends the operator's STT, LLM and TTS budget. The website demo needed
Turnstile and per-IP caps for exactly this reason. Builder users who have no
server of their own get one: their agent's generated backend mints tokens
(builder-api ADR-0026).

**The client lives in its own MIT repo,** `turncall-js` (not yet created): a
wrapper over Pipecat's JS client for the SmallWebRTC transport, with
session-token auth, call events and a drop-in widget that takes a token-minting
URL. Generated Python and TypeScript server SDKs from `openapi.json` come next;
mobile clients when someone asks.

## Consequences

- `/v1/webrtc/connect` accepts either a project API key (servers, as today) or a
  session token (browsers).
- A token carries no secret the browser could reuse elsewhere; a leaked one is
  worth one call for five minutes.
