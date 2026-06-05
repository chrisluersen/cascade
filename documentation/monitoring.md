# Monitoring

`cascade status` prints a live, per-provider dashboard — rating, health (circuit-breaker
state), key pool size, and rolling latency. Run it any time:

```bash
cascade status
cascade status --json   # raw JSON for scripts
```

## `/v1/status`

All the same data is available as JSON for programmatic consumption:

```bash
curl -H "Authorization: Bearer sk-cascade-1" http://localhost:8319/v1/status
```

## `/metrics`

OpenMetrics-compatible Prometheus endpoint at `/metrics`. No auth required by default;
set `METRICS_REQUIRE_AUTH=1` in `.env` to require the proxy API key.

Exposed metrics:

| Metric | Type | Description |
|---|---|---|
| `cascade_uptime_seconds` | gauge | Seconds since cascade started |
| `cascade_providers` | gauge | Number of configured providers |
| `cascade_keys` | gauge | Total API keys across all providers |
| `cascade_provider_rating` | gauge | Capability rating per provider |
| `cascade_provider_healthy` | gauge | 1 if circuit breaker is closed, 0 if open |
| `cascade_provider_keypool` | gauge | Number of keys available per provider |
| `cascade_provider_latency_ms` | gauge | Rolling average latency per provider |
| `cascade_requests_total` | counter | Total requests handled |
| `cascade_failovers_total` | counter | Times cascade fell back to another provider |
| `cascade_requests_in_flight` | gauge | Currently processing requests |
| `cascade_cache_hits` | counter | Cache hits |
| `cascade_cache_misses` | counter | Cache misses |
| `cascade_cache_entries` | gauge | Current cache size |

## `/health`

Simple health check (returns `{"status": "ok"}` with a 200):

```bash
curl http://localhost:8319/health
```

This endpoint is deliberately **unauthenticated** so load balancers can use it without
setup.

## Logs

Log level is set with `LOG_LEVEL` in `.env` (`INFO` by default). Logs go to stdout (the
terminal where cascade is running, or systemd journal).

Each request logs a line like:
```
2026-03-30 14:22:01 | openrouter  | 200 |  2.1s |    324 tokens | model-1 → model-2
```
which reads as: provider `openrouter`, HTTP 200, 2.1 seconds, 324 output tokens (or "stream"
for streaming requests), and a failover chain if more than one provider was tried.
