# Routing Specification

**Status:** Stable (implemented in cascade)
**RFC 2119 keywords:** MUST, SHOULD, MAY

## 1. Provider Cascade

- Router MUST support at least 3 provider tiers (primary, fallback, emergency)
- Router MUST have a per-provider timeout before falling back (default 15s)
- Router SHOULD cache successful provider routes for 60s
- Router MUST implement circuit breakers for failing providers

## 2. Model Selection

- Router MUST support per-profile model overrides
- Default model SHOULD be `deepseek/deepseek-v4-flash`
- Router MUST support prompt-based routing to specific models/providers

## 3. Security

- Provider configuration MUST NOT be committed to version control (use `.env` or machine-local config)
- API keys MUST be loaded from environment variables or a `.env` file
- The router MUST NOT expose credentials in logs or error responses

## Reliability contract updates

- Candidate capability tier precedes cost tier; cost is preferred within the
  same capability tier. Ratings are heuristics, not measured quality guarantees;
  weaker candidates remain fallback options and non-free spend can increase.
  Explicit pins retain their existing behavior. Deployment needs owner approval
  of the potential spending change.
- Free-only filtering requires both a free provider tier and an exact model ID
  with a zero price entry. Unknown aliases do not inherit substring prices and
  may drop from free-only routing. Model-only prices are estimates, not
  account/provider/quota-specific billing guarantees.
- Nonempty modern `tools` requests require literal `supports_tools=True`, set
  by a successful probe or an operator override applied during an eligible startup
  probe pass for a provider with a key. Fresh persisted state covering every
  configured provider skips that pass before reading changed overrides; restart
  alone does not apply them. Absent/stale/incomplete state, or disabled state TTL,
  reaches the startup probe path, not a timer at TTL expiry. Triggering probes can
  contact providers, consume quota, and incur charges; obtain separate deployment/
  spend approval. Clearing response cache does not clear persisted provider state.
  If no eligible provider
  has it, the OpenAI response returns HTTP 503 with
  `tool_capability_unavailable` in the error object, rather than sending plain
  chat. Anthropic errors use the Anthropic-shaped adapter; its response is not
  promised to preserve the OpenAI `code` field. Legacy `functions` and
  `function_call`, and tool-history-only semantics, are outside this gate.
  Unverified startup/probe state can cause more 503s; no live probe is implied.
- Cache identity is detached from the upstream payload and based on the
  normalized original request, endpoint family, and free-only policy before
  adaptive tokens or prompt routing mutate the upstream payload. Omitted
  `stream` and explicit `false` can remain distinct within the nested request;
  maximal equivalent-request deduplication is not guaranteed. Cache entries
  do not dynamically revoke capabilities; restart after approved routing,
  catalog, or policy changes. This is not multi-tenant cache isolation.
- Unknown pricing is not a free-price assertion. Interpret legacy numeric cost
  estimates together with the route-log `priced` field.

## Related

- See [`configuration.md`](configuration.md) for provider setup
- See [`monitoring.md`](monitoring.md) for health checks and alerts
- See [`concepts.md`](concepts.md) for architecture overview