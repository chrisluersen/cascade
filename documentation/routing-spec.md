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

## Related

- See [`configuration.md`](configuration.md) for provider setup
- See [`monitoring.md`](monitoring.md) for health checks and alerts
- See [`concepts.md`](concepts.md) for architecture overview