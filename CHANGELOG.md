# Changelog

## Unreleased

- Normalize Anthropic return values and consolidate response caching, with
  detached request identities scoped to endpoint family and free-only policy.
- Match model prices by exact ID; unknown estimates are unpriced, not free.
  Free-only routing can lose previously substring-matched aliases until exact
  prices are verified. Prices remain model-only, account-dependent estimates.
- Prefer capability tier before cost tier while keeping weaker fallbacks and
  existing explicit-pin behavior. This can increase paid use and requires
  deployment owner acceptance; heuristic ranking is not a quality benchmark.
- Fail closed for modern tool requests without probed or explicitly overridden
  tool support (503 when no eligible candidate remains). Unknown tool support
  now fails closed; legacy function fields are outside this check.
- Tool overrides apply only on keyed startup probe passes; fresh complete persisted
  state skips them, so restart alone does not apply changed overrides. Triggering
  probes needs separate deployment/spend approval (quota and charges possible).
- Add deterministic offline tests and an offline accepted-task evaluation CLI.
  Receipt completeness and quality are caller judgments, not CLI certification;
  no live provider, paid model/topology, Docker runtime, or deployment
  verification is implied.

## v0.1.0 — 2026-07-04

Initial public release.

### Features
- Multi-provider failover across 15+ providers (6 cost tiers: free → paid → local)
- Dual API support — OpenAI and Anthropic SDK without client changes
- Prompt-based routing — keyword-matched model pinning (code→DeepSeek, creative→GPT-4o, fast→cheapest)
- Smart complexity routing — request scored 1–5, matched to capability-rated models
- Credential pooling — multiple API keys per provider, round-robin, per-key cooldown
- Circuit breaker — unhealthy providers auto-removed, re-probed after cooldown
- Response caching — in-memory LRU cache (TTL-based)
- Adaptive max_tokens — auto-scales output budget by input length
- Tool-aware routing — only function-calling requests go to providers that support it
- Payload ceiling detection — skips providers whose context/output limits a request exceeds
- Reasoning model support — extra token headroom for thinking models
- Thinking field stripping — removes reasoning fields that break non-Claude providers
- Embeddings routing — multi-provider with failover (Gemini, Mistral, OpenAI, Cohere)
- Model auto-discovery — probes /models endpoint, fixes stale or renamed models
- Anthropic ↔ OpenAI translation — transparent /v1/messages ↔ /v1/chat/completions with tool mapping
- Observability — Prometheus /metrics, /v1/status dashboard, per-provider latency stats
- Key management — auth.json credential store + .env fallback, CLI-managed

### Known limitations
- Single-file architecture (~2300 lines) — designed for simplicity, not horizontal scale
- No Docker image — runs via Python directly or watchtower systemd-style service