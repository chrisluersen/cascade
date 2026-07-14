<svg xmlns="http://www.w3.org/2000/svg" width="800" height="200" viewBox="0 0 800 200">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0a0a1a"/>
      <stop offset="100%" stop-color="#141428"/>
    </linearGradient>
    <linearGradient id="line" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#6366f1"/>
      <stop offset="50%" stop-color="#a78bfa"/>
      <stop offset="100%" stop-color="#818cf8"/>
    </linearGradient>
  </defs>
  <rect width="800" height="200" fill="url(#bg)" rx="16"/>
  <text x="60" y="85" font-family="system-ui, sans-serif" font-size="56" font-weight="700" fill="#e0e7ff">cascade</text>
  <text x="60" y="125" font-family="system-ui, sans-serif" font-size="18" fill="#94a3b8">Intelligent AI request routing — failover across 15+ providers</text>
  <rect x="60" y="145" width="680" height="3" rx="1.5" fill="url(#line)"/>
  <text x="60" y="175" font-family="system-ui, monospace" font-size="13" fill="#64748b">OpenAI · Anthropic · Gemini · Groq · Mistral · Cohere · OpenRouter · and more</text>
</svg>

**cascade** is an intelligent AI inference router. It sits between your application and a pool of LLM providers — routing each request to the best provider based on capability, cost, real-time health, and prompt content. When one provider is rate-limited or down, it automatically fails over to the next.

```
  Your App ──────► cascade ──► Gemini → SambaNova → Groq → Mistral → (tries each until one works)
 (OpenAI SDK or    :8319        ↓
  Anthropic SDK)               └──► Automatic failover, key rotation, circuit breakers
```

**Why cascade, not a single provider?** Free tiers are generous but unreliable — rate limits, outages, model deprecations. cascade keeps you online by spreading across them, and only falls through to paid providers when every free option is exhausted.

## Features

| | |
|---|---|
| **Multi-provider failover** | 15+ providers across 6 cost tiers — free → paid → local |
| **Dual API support** | OpenAI **and** Anthropic SDK — plug-and-play, no client changes |
| **Prompt-based routing** | Keyword-matched model pinning (code→DeepSeek, creative→GPT-4o, fast→cheapest) |
| **Smart complexity routing** | Request scored 1–5, matched to capability-rated models |
| **Credential pooling** | Multiple API keys per provider, round-robin, per-key cooldown |
| **Circuit breaker** | Unhealthy providers auto-removed, re-probed after cooldown |
| **Response caching** | In-memory LRU cache (TTL-based) — saves free-tier quota |
| **Adaptive max_tokens** | Auto-scales output budget by input length |
| **Tool-aware routing** | Only tool-call requests go to providers that support function calling |
| **Payload ceiling detection** | Skips providers whose context/output limits a request exceeds |
| **Reasoning model support** | Extra token headroom for thinking models before they answer |
| **Thinking field stripping** | Removes reasoning fields that break non-Claude providers |
| **Embeddings routing** | Multi-provider with failover — Gemini, Mistral, OpenAI, Cohere |
| **Model auto-discovery** | Probes /models endpoint, fixes stale or renamed models |
| **Anthropic ↔ OpenAI translation** | Transparent /v1/messages ↔ /v1/chat/completions with tool mapping |
| **Observability** | Prometheus /metrics, /v1/status dashboard, per-provider latency stats |
| **Key management** | `auth.json` credential store + `.env` fallback — CLI-managed |

## Architecture

A single Python file (~2300 lines) running Flask/Waitress. One request flows through:

```
  ┌──────────┐   OpenAI-format request    ┌──────────────────────────────────────────────┐
  │ Your app │ ─────────────────────────► │                  cascade                      │
  └──────────┘   Bearer PROXY_API_KEYS    │                                              │
       ▲                                   │  1. Auth check (constant-time token compare)  │
       │                                   │  2. Cache lookup (SHA-256, LRU eviction)     │
       │         OpenAI-format response    │  3. Complexity scoring (1–5 heuristic)        │
       └────────────────────────────────► │  4. Prompt-route keyword matching             │
                                           │  5. Provider ordering (cost + capability fit) │
                                           │  6. Failover loop (key rotation → cascade)   │
                                           └──────────────────────┬───────────────────────┘
                                                                  │ first successful response
                                          ┌───────────────────────▼───────────────────────┐
                                          │ cohere  cerebras  nvidia  mistral  sambanova  │
                                          │ groq  github  gemini  openrouter  openai  ...  │
                                          │ anthropic  ollama  z.ai  naga  huggingface     │
                                          └───────────────────────────────────────────────┘
```

**Request lifecycle:**
1. Auth check against `PROXY_API_KEYS` (constant-time comparison)
2. Cache hit? Return cached response (identical requests save quota)
3. Score complexity (1=critical → 5=trivial) by token count and keywords
4. Match prompt keywords to routing rules (code, creative, fast, complex, long-context)
5. Order providers: cheapest capable model first, overkill next, underpowered last
6. Try each provider — rotate keys, handle rate limits, cascade on failure
7. Return first successful response (or `All providers exhausted`)

## Supported Providers

| Tier | Providers | Cost |
|------|-----------|------|
| **Free** | Gemini, OpenRouter (:free), SambaNova, GitHub Models, Cerebras, Groq, Mistral, Cohere, Z.ai (GLM), Naga, NVIDIA NIM, HuggingFace, Ollama | $0 |
| **Paid** | OpenAI, Anthropic, DeepSeek (via OpenRouter), Nous Portal | per-token |
| **Local** | Ollama (any local model) | $0 |

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/chrisluersen/cascade/main/get.sh | bash
cascade setup
```

Then use any OpenAI SDK client:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8319/v1", api_key="sk-cascade-1")
resp = client.chat.completions.create(
    model="cascade",          # cascade picks the best provider automatically
    messages=[{"role": "user", "content": "Hello!"}],
)
```

Or Anthropic SDK — same endpoint:

```python
import anthropic
client = anthropic.Anthropic(api_key="sk-cascade-1", base_url="http://localhost:8319")
msg = client.messages.create(
    model="claude-sonnet-5",  # model name is a hint — cascade may route elsewhere
    max_tokens=100,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

## Commands

| Command | Action |
|---|---|
| `cascade setup` | Interactive first-run: add a key, verify, start |
| `cascade start` | Start the server |
| `cascade status` | Live dashboard — per-provider health, latency, cache stats |
| `cascade auth add <provider>` | Add API keys for a provider |
| `cascade auth list` | Show all configured keys |
| `cascade model list` | Show active models per provider |
| `cascade model set <provider> <model>` | Override a provider's model |
| `cascade model reset <provider>` | Revert to default model |
| `cascade restart` | Reload config and keys |
| `cascade doctor` | Diagnose installation |
| `cascade update` | Update to the latest version |
| `cascade version` | Show installed version |

## Documentation

- **[Getting started](documentation/getting-started.md)** — zero-experience guide
- **[Usage](documentation/usage.md)** — OpenAI SDK, Anthropic SDK, tool use, embeddings
- **[Configuration](documentation/configuration.md)** — `.env` settings, `auth.json`, model overrides
- **[Providers](documentation/providers.md)** — sign-up links, capabilities, rate limits
- **[Monitoring](documentation/monitoring.md)** — `cascade status`, Prometheus `/metrics`, `/v1/status`
- **[Build an agent](documentation/build-an-agent.md)** — chatbot → memory → tools
- **[Concepts](documentation/concepts.md)** — plain-language glossary of every term you'll encounter
- **[Routing spec](documentation/routing-spec.md)** — provider cascade, timeouts, model selection

## License

MIT
