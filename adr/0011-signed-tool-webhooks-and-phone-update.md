# 0011 — Signed tool webhooks; in-place phone number update; agent delete

Three small API-surface gaps, found while building the TurnCall Builder on top
of the public API, fixed natively so every customer benefits:

## Signed custom-tool webhooks

Custom webhook tools POSTed `{tool_name, arguments, call_id, project_id}` with
no authenticity proof — any receiver had to accept unauthenticated traffic.
`ToolDefinition` gains an optional `webhook_secret` (min 16 chars); when set,
the tool POST carries `X-TurnCall-Signature: v1=<hex>` + `X-TurnCall-Timestamp`,
HMAC-SHA256 over `"{timestamp}.{body}"` — the exact scheme event webhooks and
call-init already use, so receivers verify all three with one function. The
signature is computed over the exact bytes sent (`content=`, not `json=`).
Unset secret = unsigned POST, fully backward compatible.

## `PUT /v1/phone-numbers/{id}`

The only way to re-route a bound number was unbind + rebind, which mints a new
phone id and a new `server_url_secret` — silently invalidating any call-init
endpoint verifying against the old secret. The new PUT updates
`routing_target_type/_id`, `server_url`, `sms_enabled`, `metadata` in place:
same row id, same secret. A secret is minted only when `server_url` appears on
a number that never had one. Rotation never happens implicitly.

## `DELETE /v1/agents/{id}`

There was no delete at all. DELETE archives (`state="archived"`, any prior
state) rather than hard-deleting, because calls, transcripts, and analyses
reference the agent row — history stays queryable. `agent_repo.retire_agent`
exists alongside `archive_agent` (publish-flow only archives published
versions; retire is unconditional).
