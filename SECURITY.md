# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's [private vulnerability
reporting](https://github.com/kobikis/turncall/security/advisories/new). The
report goes only to the maintainers, and you can discuss the issue and a fix
before anything becomes public.

## What to expect

TurnCall is a small project. Setting expectations honestly:

- **Acknowledgement**: within 7 days.
- **Assessment**: within 30 days, we'll tell you whether we consider it a
  vulnerability and what we intend to do.
- **Disclosure**: we aim to ship a fix and publish an advisory within **90
  days** of your report. If we need longer, we'll say so and explain why.
- **No bug bounty.** We have no budget for one. We will credit you in the
  advisory unless you'd rather stay anonymous.

If you don't hear back within 7 days, the report was probably missed — please
ping the issue tracker with no details, just asking someone to check the
advisory queue.

## Supported versions

Only the latest release receives security fixes. There are no long-term
support branches.

## Scope

TurnCall handles telephony credentials, webhook signatures, and customer audio.
Reports we're particularly interested in:

- Bypassing API key authentication or project scoping (one project reading
  another's calls, agents, or recordings).
- Forging webhook signatures, or the bootstrap endpoints accepting a request
  without a valid `X-Platform-Key`.
- SSRF through BYOM `base_url`, custom tool `webhook_url`, or MCP server URLs.
- Leaking provider API keys, recordings, or transcripts through the API or logs.
- SQL injection, or prompt injection that reaches a tool with real-world effect
  (transferring a call, invoking a webhook tool).

Out of scope: vulnerabilities in third-party providers (Twilio, OpenAI,
Deepgram and friends) — report those to the provider. Also out of scope is
anything requiring a self-hoster to have already misconfigured their own
deployment, such as running with `PLATFORM_API_KEY=dev-platform-key` in
production, which the docs explicitly warn against.

## For self-hosters

If you run TurnCall yourself, the two settings that matter most:

- Set `API_KEY_HASH_SECRET` to a strong unique value. The default gives no real
  protection, and rotating it later invalidates existing keys.
- Set `PLATFORM_API_KEY`. It gates project and first-API-key creation. It fails
  closed when empty, but the shipped dev value (`dev-platform-key`) is public
  knowledge — change it.
