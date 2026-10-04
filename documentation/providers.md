# Providers

cascade routes across a pool of providers. You only need **one** key to start —
add more (and more providers) to stay online longer. You can stack quota by creating
multiple keys per provider, and by signing up with multiple Google/GitHub accounts.

Add keys with `cascade auth add <provider>` (see [configuration.md](configuration.md) for where
they're stored).

> **Key resolution (2026-10-04).** cascade reads keys from `auth.json`, then Bitwarden,
> then `.env`. For each provider it looks up the env-var name **exactly**, then the
> singular form (strip a trailing `S`), then the alias table `_BW_ENV_ALIASES`. All
> matches are returned so the key pool can rotate — a stale secret whose name matches
> the plural form no longer shadows a good singular one. At request time a provider
> rotates to its next key on **401/403** (bad credential) and **429** (rate limit);
> a 400 skips the provider.

## Free providers

| Provider | Free tier | Sign up |
|---|---|---|
| Gemini | Generous per-minute limits (~20 req/day on the free tier) | [aistudio.google.com](https://aistudio.google.com) |
| OpenRouter | 50 requests/day per key (`:free` model IDs only) | [openrouter.ai](https://openrouter.ai) |
| SambaNova (direct) | Free tier — 20 RPM, 200K TPD | [cloud.sambanova.ai](https://cloud.sambanova.ai) |
| Groq | Fast inference, free tier | [console.groq.com](https://console.groq.com) |
| Z.ai (GLM) | ~1k requests/day | [z.ai](https://z.ai) |
| Naga AI | 100 requests/day per key | [naga.ac](https://naga.ac) |
| NVIDIA NIM | 40 requests/min per key | [build.nvidia.com](https://build.nvidia.com) |
| Hugging Face | ~$0.10/mo credit (PRO: $2/mo) — 45k+ models | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) |
| Ollama | Local, unlimited | [ollama.com](https://ollama.com) |
| Together | Free tier | [together.ai](https://together.ai) |
| SiliconFlow | Free tier — 30 RPM, 60K TPM | [cloud.siliconflow.cn](https://cloud.siliconflow.cn) |
| Aion Labs | Free tier | [aionlabs.ai](https://aionlabs.ai) |

**Retired / unusable — do not re-add without re-verifying (measured 2026-10-04):**

| Provider | Why |
|---|---|
| GitHub Models | Inference endpoint retired (HTTP 000 — connection failure). |
| Cerebras | Free tier now requires billing (HTTP 402 on a valid key). |
| Mistral / Cohere / "SambaNova via OpenRouter" | Only reachable through OpenRouter, and only on paid model IDs — removed as paid and as a spend risk. |
| DeepInfra | 402 "needs positive balance" — free tier gone. |
| Longcat | 402 "insufficient token quota". |

> **Hugging Face note:** one token reaches 45,000+ models across many inference partners
> via an OpenAI-compatible endpoint. The free credit is small, so it's best as an *extra* in
> the pool (cascade fails over to other providers when it runs out). The default model
> uses the `:cheapest` suffix to stretch the credit; change it with `HUGGINGFACE_MODEL`.
>
> **Key-name gotcha:** the Bitwarden secret named `HUGGINGFACE_API_KEYS` is **stale** (401).
> The working value lives under `HF_TOKEN`. Both are now collected and the pool rotates,
> but prefer `HF_TOKEN`.

## Paid providers

Add your existing API key; cascade handles everything else.

| Provider | Default model | API keys |
|---|---|---|
| OpenAI | `gpt-4o-mini` | [platform.openai.com](https://platform.openai.com/api-keys) |
| Anthropic | `claude-haiku-4-5` | [console.anthropic.com](https://console.anthropic.com) |

> Anthropic's API uses a different wire format from OpenAI. cascade translates
> automatically — your app sends the same OpenAI-format request regardless of which
> provider handles it.

## Valid provider names

Use these names with `cascade auth add`, `cascade model set`, and the `<PROVIDER>_*` environment
variables:

`gemini`, `openrouter`, `groq`, `zai`, `naga`, `nvidia_nim`, `huggingface`, `together`,
`sambanova_direct`, `siliconflow`, `aion`, `ollama`, `openai`, `anthropic`,
`deepinfra`, `fireworks`, `llm7`, `ovhcloud`, `longcat`, `nemotron-ultra-free`,
`deepseek-v4-flash`, `glm-5.2`, `deepseek-v4-pro`, `sonnet-5`, `sonnet-4.6`,
`mimo-v2.5`, `hy3-preview`, `minimax-m3`.

## Provider health (measured)

Re-measure any time with:

```bash
# Full two-plane probe (control + data), machine-readable
python3 scripts/cascade-probe.py --json
# Per-provider availability from the last probe
python3 -c "import json;d=json.load(open('projects/cascade/cascade_state.json'));\
p=d['providers'];print(sum(1 for v in p.values() if v['available']),'of',len(p));\
[print(' ',k,v['model']) for k,v in sorted(p.items()) if v['available']]"
# Force a fresh probe on next start (ignore the state TTL)
CASCADE_FORCE_REPROBE=1 bash scripts/cascade-up.sh
```

Measured 2026-10-04 (9 of 27 available; `*` = also supports tools + reasoning):

| Provider | Model | Notes |
|---|---|---|
| groq * | `openai/gpt-oss-120b` | was 404 on the retired `llama-3.3-70b-versatile` |
| huggingface * | `openai/gpt-oss-120b:cheapest` | was masked by the stale `HUGGINGFACE_API_KEYS` |
| nvidia_nim * | `nvidia/nemotron-3-super-120b-a12b` | |
| openrouter * | `nvidia/nemotron-3-super-120b-a12b:free` | |
| nemotron-ultra-free * | `nvidia/nemotron-3-ultra-550b-a55b:free` | |
| together * | `Qwen/Qwen3.5-9B` | |
| zai | `glm-4.5-flash` | |
| aion | `aion-labs/aion-2.0` | |
| ollama | `qwen3.5:9b-16k` | local |

Volatile by nature — expect the count to move:
**Gemini** and **Naga** are quota-limited (they drop and return as quota resets);
**ovhcloud** is anonymous-tier at 2 RPM.

> **State freshness.** The probe result is cached in `cascade_state.json` for
> `CASCADE_STATE_TTL_HOURS` (default **4h**). Set it to `0`, or start once with
> `CASCADE_FORCE_REPROBE=1`, to re-probe immediately. Short TTLs matter: with the old
> 24h default a single transient probe failure hid a working provider for a full day.

## Per-provider capabilities

Each provider's model is probed at startup for **function-calling** and **reasoning**
support; results show up in `cascade status` and `/v1/status`. See
[usage.md](usage.md) for how those affect tool routing, and
[configuration.md](configuration.md) for the override variables.