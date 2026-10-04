# Configuration

All configuration is via environment variables (in `.env`) and the `auth.json` credential
store. Everything is optional with sensible defaults — cascade runs out of the box once
it has at least one key.

## Where your keys live

`cascade auth add` writes to **`auth.json`** — cascade's own credential store, kept next to
the router. It's git-ignored, so real keys are never committed.

```json
{
  "providers": {
    "openrouter": ["sk-or-key1", "sk-or-key2"],
    "gemini": ["AIzaSy-key"]
  }
}
```

> Keys in `.env` (e.g. `OPENROUTER_API_KEYS=k1,k2`) still work too — cascade reads
> `auth.json` first, then falls back to `.env`. Point at a different file with
> `CASCADE_AUTH_FILE=/path/to/auth.json`.

## Settings (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8319` | Port to listen on |
| `PROXY_API_KEYS` | `sk-cascade-1` | Comma-separated keys your app uses to authenticate |
| `CASCADE_AUTH_FILE` | `./auth.json` | Where keys are stored |
| `CACHE_TTL_SECONDS` | `300` | Response cache lifetime (`0` disables) |
| `CASCADE_STATE_TTL_HOURS` | `24` | Persisted provider-state freshness at startup (`0` bypasses freshness skip) |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `METRICS_REQUIRE_AUTH` | `0` | Require the proxy key on `/metrics` (`1` to enable) |
| `REASONING_TOKEN_RESERVE` | `4096` | Extra output budget added for reasoning models so hidden chain-of-thought doesn't eat the answer (`0` disables) |

### Per-provider model

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Model override (set via `cascade model set`) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model override (set via `cascade model set`) |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Model override (set via `cascade model set`) |
| `<PROVIDER>_MODEL` | *(varies)* | Same pattern applies to all providers |

### Per-provider embeddings

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | Embedding model (empty disables this provider for `/v1/embeddings`) |
| `<PROVIDER>_EMBED_MODEL` | *(gemini/mistral/cohere set)* | Same pattern for embeddings; set empty to disable |

### Per-provider capability overrides

At startup, cascade probes when provider state is absent, stale, or missing a
configured provider; a fresh state covering all configured providers skips probes.
This is startup logic, not a timer that probes a running service at TTL expiry.

| Variable | Default | Purpose |
|---|---|---|
| `<PROVIDER>_SUPPORTS_TOOLS` | *(auto-probed on eligible pass)* | Set tool-capability on/off (`1`/`0`) only during a startup probe pass for a provider with a key; fresh complete state bypasses it |
| `<PROVIDER>_REASONING` | *(auto-probed)* | Force reasoning-model on/off (`1`/`0`) |
| `<PROVIDER>_SKIP_TOKENS_OVER` | *(per provider)* | Skip this provider when an estimated request exceeds this many tokens (`0` = never) |
| `<PROVIDER>_MAX_OUTPUT_TOKENS` | *(per provider)* | Clamp `max_tokens` down to this provider's output ceiling (`0` = no clamp) |
| `<PROVIDER>_REASONING_EFFORT` | `medium` *(nous_portal only)* | Thinking budget sent as `reasoning_effort` for reasoning models — cuts latency/output tokens (empty = don't send) |

## Model overrides

Each provider has a default model that works out of the box. Switch models without editing
files:

```bash
cascade model list                              # see all providers and their active model
cascade model set anthropic claude-sonnet-4-6   # upgrade Anthropic to Sonnet
cascade model set openai gpt-4o                 # use full GPT-4o instead of mini
cascade model set gemini gemini-2.5-pro         # switch Gemini to Pro
cascade model reset anthropic                   # revert back to the default
cascade restart                                 # apply changes
```

Overrides are stored as plain variables in `.env` (e.g. `ANTHROPIC_MODEL=claude-sonnet-4-6`)
and active overrides are highlighted in `cascade model list`.

## Offline verification

Set `CASCADE_OFFLINE=1` before importing the router in the offline test fixture.
It disables environment-file and auth-file loading, Bitwarden startup, provider
construction, startup probe threads, and tokenizer initialization. It does not
configure an upstream provider or make normal imports generally side-effect-free.
Normal startup remains unchanged when it is unset. See [development and
verification](development.md) for the default test command and safety scope.

## Pricing and capability caveats

`<PROVIDER>_SUPPORTS_TOOLS=1` overrides the tool probe only on an eligible startup
probe pass for a provider with a key. Fresh persisted state covering all configured
providers returns before the override is read, even if its tool flag is missing;
a restart alone does not apply a changed override. Disabling the state TTL also
avoids the freshness return. Forcing probes can contact providers, consume quota,
and incur charges; obtain separate deployment/spend approval first. Clearing the
response cache does not clear persisted provider state. Without a successful
probe or applied override, modern `tools` requests cannot use that provider;
unverified state may yield additional 503s. Legacy `functions`/`function_call`
and tool-history-only requests are outside this gate. The offline suite does
not establish a live provider probe.

Model price lookup requires an exact model ID. An unlisted model has a legacy
zero numeric estimate but is unpriced, not free; free-only requires both a free
provider tier and an explicit zero model price. Previously substring-matched
aliases may no longer qualify until a verified exact entry is added. Listed
prices are model-only estimates and do not certify provider/account/quota billing.
