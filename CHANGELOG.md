# Changelog

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