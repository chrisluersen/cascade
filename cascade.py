#!/usr/bin/env python3
"""
cascade — Free-tier AI model cascade with automatic key rotation.

A lightweight OpenAI-compatible proxy that:
  - Rotates across multiple API keys per provider automatically
  - Cascades to the next provider when one is exhausted or rate-limited
  - Strips thinking/reasoning fields that break non-Claude providers
  - Handles 413 (payload too large) by cascading instead of crashing
  - Caches identical responses to preserve free-tier quota
  - Routes short requests to low-latency providers first (optional)
  - Tracks per-provider latency and error rates

Supported providers (configure via .env or auth.json):
  Free (OpenRouter):  Cohere · Cerebras · Nvidia · Mistral · SambaNova
  Free (direct):      Nvidia NIM · SambaNova Direct · Groq · GitHub Models · Gemini
  Free (direct):      Together · LLM7.io · OVHcloud · LongCat · SiliconFlow · AI Hub Mix · Aion Labs
  Free (native):      Z.ai (GLM) · Naga AI · DeepInfra · Fireworks · HuggingFace
  Local:              Ollama
  Paid:               OpenAI · Anthropic · OpenRouter (paid models) · Nous Portal

Quick start:
  pip install -r requirements.txt
  cp .env.example .env   # add your API keys
  python cascade.py
"""

import json, os, time, threading, logging, hashlib, hmac, itertools, uuid as _uuid, subprocess, shutil
from pathlib import Path
from collections import deque, OrderedDict
from flask import Flask, request, jsonify, Response, stream_with_context
import requests

# ── Config ─────────────────────────────────────────────────────────────────────

def _load_env(path: str = ".env"):
    """Load key=value pairs from a .env file into os.environ (no-op if missing)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("cascade")

# Shared HTTP session — reuses TCP/TLS connections to each provider host across
# requests (HTTP keep-alive), so we don't pay a fresh ~100–300ms handshake on
# every call. Thread-safe for sending; pool_maxsize covers our worker threads.
# max_retries=0 because the cascade handles retries, not urllib3.
_HTTP = requests.Session()
_http_adapter = requests.adapters.HTTPAdapter(
    pool_connections=20,
    pool_maxsize=max(32, int(os.environ.get("WORKER_THREADS", 16)) * 2),
    max_retries=0,
)
_HTTP.mount("https://", _http_adapter)
_HTTP.mount("http://", _http_adapter)

PORT              = int(os.environ.get("PORT", 8319))
CASCADE_API_KEY    = [k.strip() for k in os.environ.get("CASCADE_API_KEY", "").split(",") if k.strip()]
CASCADE_MODEL      = os.environ.get("CASCADE_MODEL_ID", "cascade")
CACHE_TTL         = int(os.environ.get("CACHE_TTL_SECONDS", 600))   # 0 = disabled
CACHE_MAX_SIZE    = int(os.environ.get("CACHE_MAX_SIZE", 500))
PROMPT_ROUTE_RULES = [
    # Code -> cheap capable model
    {"match": ["code", "python", "debug", "function ", "class ", "import ", "```", "fix", "refactor", "test", "pytest", "unittest", "exception", "traceback"], "tier": 1, "model": "deepseek/deepseek-v4-flash", "label": "code", "negative": []},
    # Creative/writing -> OpenAI
    {"match": ["creative", "write", "story", "essay", "blog", "poem", "script"], "tier": 1, "model": "openai/gpt-4o", "label": "creative", "negative": []},
    # Fast/simple -> cheapest capable model
    {"match": ["fast", "simple", "quick", "brief", "one sentence", "yes or no", "what is", "who is", "define", "spell", "explain simply", "tldr"], "tier": 1, "model": "deepseek/deepseek-v4-flash", "label": "fast", "negative": ["translate to python", "convert to python", "migrate to python"]},
    # Complex engineering -> medium/premium
    {"match": ["architect", "design", "implement", "refactor", "algorithm", "optimize", "research", "step by step", "walk me through", "plan", "review", "compare", "analyze"], "tier": 2, "model": "anthropic/claude-sonnet-5", "label": "complex", "negative": []},
    # Long-context -> large context models
    {"match": ["context", "long", "document", "migrate", "convert", "summarize"], "tier": 1, "model": "minimax-m3", "label": "long_context", "negative": ["summarize in one sentence", "tldr"]},
    # Removed overly greedy fallback; let cost cascade handle greetings.
]

def _pick_model_by_prompt(messages: list) -> str | None:
    content = " ".join(
        m["content"] if isinstance(m.get("content"), str) else " ".join(p.get("text", "") for p in m["content"] if isinstance(p, dict))
        for m in messages if m.get("content")
    )
    cl = content.lower()
    tokens = len(content) // 4
    has_tools = any(bool(m.get("tools")) for m in messages)

    for rule in PROMPT_ROUTE_RULES:
        if any(k in cl for k in rule.get("match", [])):
            if any(n in cl for n in rule.get("negative", [])):
                continue
            tier = rule.get("tier", 1)

            # Tool-heavy prompts should avoid basic free-tier models.
            if has_tools and tier == 0:
                continue
            # Long prompts should not be force-routed to tiny free models.
            if tier == 0 and tokens > 400:
                continue
            return rule["model"]
    return None


FAST_ROUTE_TOKENS = int(os.environ.get("FAST_ROUTE_THRESHOLD", 200)) # 0 = disabled, 50-200 recommended
STATE_FILE        = Path(os.environ.get("CASCADE_STATE_FILE", "./cascade_state.json"))
STATE_TTL_HOURS   = int(os.environ.get("CASCADE_STATE_TTL_HOURS", 24))  # 0 = re-probe every start
AUTH_FILE         = Path(os.environ.get("CASCADE_AUTH_FILE", "./auth.json"))  # cascade's own key store

# ── Cost tracking (USD per 1M tokens) ──────────────────────────────────────
# Input/output pricing for every model cascade routes through. Missing entries
# fall back to $0 (free-tier safe default). Sourced from provider pricing pages
# and OpenRouter pricing (2026-07).
# Usage is tracked per-provider and exposed at /v1/status + /metrics.
KNOWN_MODEL_COSTS: dict = {
    # Google — all free tier
    "gemini-2.5-flash-lite":        (0.0,    0.0),
    "gemini-2.5-flash":             (0.0,    0.0),      # free on free tier
    "gemini-embedding-001":         (0.0,    0.0),

    # OpenAI via GitHub — free
    "gpt-4o":                       (0.0,    0.0),      # free via GitHub Models
    "gpt-4o-mini":                  (0.0,    0.0),      # free via GitHub Models

    # Groq — free tier
    "llama-3.3-70b-versatile":      (0.0,    0.0),

    # Direct free providers
    "gpt-oss-120b":                 (0.0,    0.0),      # ovhcloud, cerebras free
    "DeepSeek-V3.2":                (0.0,    0.0),      # sambanova free
    "DeepSeek-V2.5":                (0.0,    0.0),      # siliconflow free
    "nemotron-3-super-120b-a12b":   (0.0,    0.0),      # nvidia_nim free
    "nemotron-3-ultra-550b-a55b:free": (0.0, 0.0),      # OpenRouter free
    "nemotron-3-super-120b-a12b:free": (0.0, 0.0),      # naga free, OpenRouter fallback
    "openai/gpt-oss-120b:cheapest": (0.0,    0.0),      # huggingface free
    "Qwen/Qwen2.5-72B-Instruct":    (0.0,    0.0),      # deepinfra free
    "accounts/fireworks/models/qwen2p5-coder-32b-instruct": (0.0, 0.0),  # fireworks free
    "LongCat-2.0":                  (0.0,    0.0),
    "coding-glm-5.2-free":          (0.0,    0.0),      # aihubmix free
    "glm-4.5-flash":                (0.0,    0.0),      # z.ai free (1000 req/day)
    "ng-nemotron-3-super":          (0.0,    0.0),      # naga free
    "aion-2.5":                     (0.0,    0.0),      # aion free

    # OpenRouter models (shared key, various pricing)
    "cohere/command-a-03-2025":     (0.0,    0.0),      # free tier
    "mistral/mistral-medium-latest":(0.0,    0.0),      # free tier
    "cerebras/gpt-oss-120b":        (0.0,    0.0),      # free tier
    "sambanova/DeepSeek-V3.2":      (0.0,    0.0),      # free tier
    "nvidia/deepseek-ai/deepseek-v4-flash": (0.0, 0.0), # free on OR

    # Paid OpenRouter models
    "deepseek/deepseek-v4-flash":   (0.098,  0.196),    # daily driver, cheapest paid
    "deepseek/deepseek-v4-pro":     (0.435,  0.87),     # frontier
    "tencent/hy3-preview":          (0.063,  0.21),     # cheapest reasoning
    "xiaomi/mimo-v2.5":             (0.105,  0.28),     # cheap, 1M ctx
    "minimax/minimax-m3":           (0.30,   1.20),     # 1M ctx, multimodal
    "z-ai/glm-5.2":                 (0.93,   3.00),     # premium Chinese model
    "anthropic/claude-sonnet-4.6":  (3.00,  15.00),     # frontier coding
    "anthropic/claude-sonnet-5":    (2.00,  10.00),     # frontier coding (cheaper than 4.6!)

    # Paid direct
    "Qwen/Qwen3.5-9B":              (0.18,   0.18),     # together paid tier
    "devstral-small-2:24b":         (0.15,   0.60),     # llm7 paid tier

    # Paid via Nous sub
    "deepseek/deepseek-v4-flash":   (0.098,  0.196),    # nous_portal, same as OR

    # Local
    "qwen3.5:9b-16k":               (0.0,    0.0),      # ollama local
    }

def _estimate_cost(prompt_tokens: int, completion_tokens: int, model: str) -> float:
    """Estimate USD cost for a request. Matches full model ID first, then falls
    back to longest-prefix match for generic entries like 'gpt-4o'. Returns 0
    for unknown/free models."""
    if not prompt_tokens and not completion_tokens:
        return 0.0
    # Exact match first
    cost = KNOWN_MODEL_COSTS.get(model)
    if cost:
        inp, out = cost
        return (prompt_tokens / 1_000_000 * inp) + (completion_tokens / 1_000_000 * out)
    # Longest-prefix substring match for generic entries
    for key, (inp, out) in sorted(KNOWN_MODEL_COSTS.items(), key=lambda x: -len(x[0])):
        if key in model:
            return (prompt_tokens / 1_000_000 * inp) + (completion_tokens / 1_000_000 * out)
    return 0.0

# ── Bulkheads ───────────────────────────────────────────────────────────────
# Max concurrent in-flight requests per provider. Prevents one slow provider
# from consuming all worker threads. 0 = unlimited (default 4).
BULKHEAD_MAX = int(os.environ.get("BULKHEAD_MAX_CONCURRENT", 4))

# ── Trace IDs ────────────────────────────────────────────────────────────────
# Every request gets a unique trace_id logged and returned as X-Trace-Id header.
TRACE_ENABLED: bool = True


def _load_auth_json() -> dict[str, list[str]]:
    """Load provider API keys from auth.json — cascade's own credential store,
    managed by `cascade auth add`. This makes cascade self-contained: keys live with
    cascade, independent of any host application.
    Managed keys always take precedence over env-var keys for the same provider.
    If auth.json is missing or empty, cascade simply falls back to keys from .env (see _keys_for).

    Returns {provider_name: [keys]}. A missing or invalid file is non-fatal —
    cascade simply falls back to keys from .env (see _keys_for)."""
    if not AUTH_FILE.exists():
        return {}
    try:
        doc = json.loads(AUTH_FILE.read_text())
        out: dict[str, list[str]] = {}
        for name, keys in doc.get("providers", {}).items():
            if isinstance(keys, list):
                out[name] = [str(k).strip() for k in keys if str(k).strip()]
        return out
    except Exception as e:
        log.warning(f"Could not read {AUTH_FILE}: {e}")
        return {}

_AUTH_KEYS = _load_auth_json()

# ── Bitwarden Secrets Manager integration ──────────────────────────────────
# When BWS_ACCESS_TOKEN is available, cascade fetches API keys from Bitwarden
# at module startup, falling back gracefully if the CLI or token is missing.

_BW_KEYS: dict[str, str] = {}  # env_var_name -> value


def _load_bitwarden_keys() -> dict[str, str]:
    """Fetch ALL secrets from Bitwarden Secrets Manager via the `bws` CLI.

    Requires BWS_ACCESS_TOKEN in the environment (loaded from cascade .env
    by _load_env() at line 36, which runs before this function).

    Returns {env_var_name: value} on success, {} on any failure (CLI not found,
    token missing, network error, JSON parse error, non-zero exit).

    The result is a flat key-value map — each Bitwarden secret's `key` field
    IS the env var name (e.g. ``OPENAI_API_KEY``), and `value` is the key.
    """
    BWS_ENV = os.environ.get("BWS_ACCESS_TOKEN", "").strip()
    if not BWS_ENV:
        log.info("BWS_ACCESS_TOKEN not set — skipping Bitwarden key loading")
        return {}

    bws_path = shutil.which("bws")
    if not bws_path:
        # bws may not be in PATH when launched from a scheduled task.
        # Check the Hermes bin directory directly.
        hermes_bin = os.path.expanduser(
            "~/AppData/Local/hermes/bin/bws"
        )
        if os.path.isfile(hermes_bin):
            bws_path = hermes_bin
    if not bws_path:
        log.warning("bws CLI not found — skipping Bitwarden key loading")
        return {}

    try:
        result = subprocess.run(
            [bws_path, "secret", "list"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "BWS_ACCESS_TOKEN": BWS_ENV},
        )
        if result.returncode != 0:
            log.warning(
                f"bws secret list exited {result.returncode}: "
                f"{result.stderr.strip()}"
            )
            return {}

        secrets = json.loads(result.stdout)
        if not isinstance(secrets, list):
            return {}

        out: dict[str, str] = {}
        for entry in secrets:
            key = (entry.get("key") or "").strip()
            value = (entry.get("value") or "").strip()
            if key and value:
                out[key] = value
        log.info(f"Loaded {len(out)} API keys from Bitwarden")
        return out
    except FileNotFoundError:
        log.warning("bws CLI not found — skipping Bitwarden key loading")
        return {}
    except subprocess.TimeoutExpired:
        log.warning("bws secret list timed out after 10s — skipping Bitwarden")
        return {}
    except json.JSONDecodeError as e:
        log.warning(f"bws output was not valid JSON: {e}")
        return {}
    except Exception as e:
        log.warning(f"Failed to load keys from Bitwarden: {e}")
        return {}


_BW_KEYS = _load_bitwarden_keys()


# ── Bitwarden env var name aliases ─────────────────────────────────────────────
# Maps cascade's internal env var names to the actual Bitwarden key names.
# Needed when the key was stored in Bitwarden under a different name than
# what cascade's _keys_for() looks up.
_BW_ENV_ALIASES: dict[str, str] = {
    "NVIDIA_NIM_API_KEY":    "NVIDIA_API_KEY",       # BW has NVIDIA_API_KEY
    "SAMBANOVA_DIRECT_API_KEY": "SAMBANOVA_API_KEY", # BW has SAMBANOVA_API_KEY
    "GLM_API_KEYS":          "ZAI_API_KEY",          # BW has ZAI_API_KEY
}
# Circuit-breaker knobs — a provider that fails health repeatedly is tripped out
# of rotation for a cooldown, then probed again (half-open). Overridable via env.
BREAKER_WINDOW      = int(os.environ.get("BREAKER_WINDOW", 8))          # recent outcomes to weigh
BREAKER_MIN_SAMPLES = int(os.environ.get("BREAKER_MIN_SAMPLES", 4))     # min samples before it can trip
BREAKER_ERROR_RATE  = float(os.environ.get("BREAKER_ERROR_RATE", 0.5))  # trip at >= this health-fail fraction
BREAKER_COOLDOWN    = int(os.environ.get("BREAKER_COOLDOWN", 60))       # seconds the breaker stays open

# Providers known for low-latency inference — promoted for short requests
_FAST_PROVIDERS = {"groq", "zai", "github_models", "gemini",
                     "sambanova_direct", "nvidia_nim", "naga",
                     "openai", "nous_portal", "deepseek-v4-flash",
                     "ovhcloud", "aion", "deepinfra", "together"}

# Per-request counter for round-robin among equally-rated providers.
# itertools.count().__next__ is atomic in CPython, so it's thread-safe.
_rr_counter = itertools.count()

# ── Smart routing: capability ratings ─────────────────────────────────────────
# 1=outstanding  2=best  3=good  4=fair  5=basic  (lower = more capable)
# Recommended base model: set CASCADE_MODEL_PROVIDER + CASCADE_MODEL_ID
# e.g. CASCADE_MODEL_PROVIDER=openai  CASCADE_MODEL_ID=gpt-4o-mini
KNOWN_MODEL_RATINGS: dict = {
    # 1 — Outstanding (frontier reasoning, coding, science)
    "gpt-5": 1, "gpt-5-codex": 1, "gpt-4o": 1, "o1": 1, "o3": 1, "o4-mini": 1,
    "claude-opus-5": 1, "claude-opus-4": 1, "claude-sonnet-5": 1, "claude-sonnet-4.6": 1,
    "deepseek-v4-pro": 1, "deepseek-r2": 1,
    "gemini-2.5-pro": 1, "gemini-2.5-flash-thinking": 1,
    "nemotron-3-ultra": 1, "qwen3-235b": 1, "qwen3-72b": 1,

    # 2 — Excellent (strong daily drivers, capable of complex tasks)
    "deepseek-v4-flash": 2, "deepseek-v4": 2,
    "deepseek-v3": 2, "deepseek-v2": 2,
    "gemini-2.5-flash": 2,
    "llama-3.3-70b": 2, "llama-3.1-70b": 2, "llama-4-maverick": 2, "llama-4-scout": 2,
    "claude-3-5": 2, "claude-haiku": 2,
    "mistral-large": 2, "command-a": 2,
    "nvidia/nemotron-3-super": 2, "nemotron": 2,
    "grok-2": 2, "grok-3": 2,
    "qwen2.5-72b": 2, "qwen3-32b": 2,
    "hy3-preview": 2, "phi-4-reasoning": 2,
    "hermes-4-405b": 2, "hermes-4-70b": 2,
    "glm-5.2": 2,

    # 3 — Good (competent, fast, good for most tasks)
    "gemini-2.5-flash-lite": 3, "gemini-1.5-flash": 3,
    "gpt-4o-mini": 3, "gpt-oss-120b": 3,
    "mistral-medium": 3, "mistral-small": 3,
    "glm-4.5-flash": 3, "glm-4.7-flash": 3,
    "llama-3.1-8b-instant": 3, "llama-3.2-3b": 3,
    "qwen2.5-32b": 3, "qwen3-14b": 3, "qwen3-8b": 3,
    "qwen3.5-9b": 3,
    "DeepSeek-V3.2": 3, "DeepSeek-V2.5": 3,
    "qwen2p5-coder-32b": 3,
    "minimax-m3": 3,
    "xiaomi/mimo-v2.5": 3,
    "devstral-small-2:24b": 3,
    "LongCat-2.0": 3,
    "hermes-4.3-36b": 3, "hermes-4.3": 3,
    "phi-4": 3,
    "coding-glm-5.2-free": 3,
    "yi-large": 3,
    "command-r-plus": 3,

    # 4 — Fair (acceptable for simple tasks, fast)
    "gemini-1.5-flash-8b": 4,
    "command-r7b": 4,
    "mistral-7b": 4,
    "qwen2.5-7b": 4, "qwen3-4b": 4,
    "phi-3.5": 4, "phi-3-medium": 4,
    "mixtral-8x7b": 4,
    "llama-4-scout": 4,
    "yi-medium": 4, "yi-6b": 4,
    "tencent/hy3-preview": 4,

    # 5 — Basic (trivial tasks, micro models)
    "phi-3-mini": 5, "phi-3.5-mini": 5,
    "yi-mini": 5,
    "qwen3-4b": 5,
    "llama-4-scout": 5,
}

_RATING_PATTERNS: list = [
    (1, ["pro-exp", "ultra", "opus", "o3", "o1-pro", "405b", "671b", "r1-zero", "sonnet-5", "sonnet-4", "v4-pro", "codex", "thinking"]),
    (2, ["70b", "large", "plus", "pro", "turbo", "super", "sonnet", "72b", "32b", "maverick", "phi-4", "wizardlm", "grok", "deepseek-v4", "hy3"]),
    (3, ["flash", "small", "mini", "medium", "120b", "8b-instant", "glm-4", "14b", "22b", "mixtral", "qwen", "yi-m", "phi-3", "v3.2", "v2.5", "mimo", "minimax", "devstral", "longcat", "coder"]),
    (4, ["7b", "8b", "lite", "fast", "r7b", "nano", "3b", "phi-3-mini", "phi-3.5-mini", "yi-mini", "4b"]),
    (5, ["micro", "tiny", "1b"]),
]
_COMPLEXITY_LABELS = {1: "critical", 2: "complex", 3: "standard", 4: "simple", 5: "trivial"}
_provider_state: dict = {}   # populated at startup by _initialize_ratings()


def _keys(env_var: str) -> list[str]:
    """Collect all keys for a provider from three naming conventions (combined + de-duped):
      1. Singular:  MISTRAL_API_KEY=k1
      2. Plural:    MISTRAL_API_KEYS=k1,k2,k3   (comma-separated)
      3. Numbered:  MISTRAL_API_KEY_2=k2, MISTRAL_API_KEY_3=k3, ...
    The plural form is the canonical multi-key env var; singular and numbered are
    convenience aliases that are merged in automatically.
    """
    collected = []
    # singular (drop the trailing S if the caller passed the plural form)
    singular = env_var[:-1] if env_var.endswith("S") else env_var
    if singular != env_var:
        single = os.environ.get(singular, "").strip()
        if single:
            collected.append(single)
    # plural / comma-separated
    for piece in os.environ.get(env_var, "").split(","):
        piece = piece.strip()
        if piece:
            collected.append(piece)
    # numbered suffixes on the singular name (_2, _3, ...)
    i = 2
    while True:
        nv = os.environ.get(f"{singular}_{i}", "").strip()
        if not nv:
            break
        collected.append(nv)
        i += 1
    seen, out = set(), []
    for k in collected:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _keys_for(provider_name: str, env_var: str) -> list[str]:
    """All keys for a provider: auth.json entries first (the primary store that
    `cascade auth add` writes to), then Bitwarden (if available), then .env keys
    as a final fallback. Deduped, order preserved.

    Bitwarden key resolution tries, in order:
    1. Exact match on the env_var name
    2. Singular form (strip trailing S) — handles OPENAI_API_KEYS → OPENAI_API_KEY
    3. Alias table for known name mismatches (e.g. GLM_API_KEYS → ZAI_API_KEY)
    """
    merged = list(_AUTH_KEYS.get(provider_name, []))

    # Bitwarden: resolve the env var name through multiple strategies
    bw_val = _BW_KEYS.get(env_var, "")
    if not bw_val:
        # Try singular form (strip trailing S)
        singular = env_var[:-1] if env_var.endswith("S") else None
        if singular and singular != env_var:
            bw_val = _BW_KEYS.get(singular, "")
    if not bw_val:
        # Try global alias table (maps cascade env_var names to BW key names)
        aliased = _BW_ENV_ALIASES.get(env_var, "")
        if aliased:
            bw_val = _BW_KEYS.get(aliased, "")
    if bw_val:
        merged.append(bw_val)
    merged += _keys(env_var)
    seen, out = set(), []
    for k in merged:
        if k and k not in seen and "«redacted" not in k:
            seen.add(k)
            out.append(k)
    return out


def _int_env(env_var: str, default: int = 0) -> int:
    """Parse an integer env var, falling back to default on missing/invalid."""
    try:
        return int(os.environ.get(env_var, default))
    except (TypeError, ValueError):
        return default


def _parse_retry_after(value, default: int = 60) -> int:
    """Parse a Retry-After header value. RFC 9110 allows either delay-seconds
    or an HTTP date; some providers also send fractional seconds. Anything we
    can't read as a number falls back to the default cooldown."""
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return default


# ── Provider definitions ───────────────────────────────────────────────────────

def _build_providers() -> list[dict]:
    providers = []
    openrouter_keys = _keys_for("openrouter", "OPENROUTER_API_KEYS")

    # ════════════════════════════════════════════════════════════
    # Tier 1 — Fast & Free (handles ~90%+ of requests)
    # ════════════════════════════════════════════════════════════

    # --- cohere (command-a-03-2025 via OpenRouter) ---
    if openrouter_keys:
        providers.append({
            "name":     "cohere",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "cohere/command-a-03-2025",
            "keys":     openrouter_keys,
            "cost":     0,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- cerebras (gpt-oss-120b via OpenRouter) ---
    if openrouter_keys:
        providers.append({
            "name":     "cerebras",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "cerebras/gpt-oss-120b",
            "keys":     openrouter_keys,
            "cost":     0,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- nvidia (deepseek-ai/deepseek-v4-flash via OpenRouter) ---
    if openrouter_keys:
        providers.append({
            "name":     "nvidia",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "nvidia/deepseek-ai/deepseek-v4-flash",
            "keys":     openrouter_keys,
            "cost":     0,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- nvidia_nim (direct NVIDIA NIM API, free dev tier — 100+ models) ---
    nvidia_nim_keys = _keys_for("nvidia", "NVIDIA_NIM_API_KEY")
    if nvidia_nim_keys:
        providers.append({
            "name":     "nvidia_nim",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "model":    os.environ.get("NVIDIA_NIM_MODEL", "nvidia/nemotron-3-super-120b-a12b"),
            "keys":     nvidia_nim_keys,
            "cost":     0,
        })

    # ════════════════════════════════════════════════════════════
    # Tier 2 — Free Large Context (overflow when fast tiers can't)
    # ════════════════════════════════════════════════════════════

    # --- mistral (mistral-medium-latest via OpenRouter) ---
    if openrouter_keys:
        providers.append({
            "name":     "mistral",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "mistral/mistral-medium-latest",
            "keys":     openrouter_keys,
            "cost":     0,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- sambanova (DeepSeek-V3.2 via OpenRouter) ---
    if openrouter_keys:
        providers.append({
            "name":     "sambanova",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "sambanova/DeepSeek-V3.2",
            "keys":     openrouter_keys,
            "cost":     0,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- sambanova_direct (direct SambaNova API, free tier — 20 RPM, 200K TPD) ---
    sambanova_direct_keys = _keys_for("sambanova", "SAMBANOVA_DIRECT_API_KEY")
    if sambanova_direct_keys:
        providers.append({
            "name":     "sambanova_direct",
            "base_url": "https://api.sambanova.ai/v1",
            "model":    os.environ.get("SAMBANOVA_DIRECT_MODEL", "DeepSeek-V3.2"),
            "keys":     sambanova_direct_keys,
            "cost":     0,
        })

    # ════════════════════════════════════════════════════════════
    # Tier 3 — Paid (only when free tiers truly can't handle it)
    # ════════════════════════════════════════════════════════════

    nous_portal_keys = _keys_for("nous_portal", "NOUS_PORTAL_API_KEYS")
    if nous_portal_keys:
        providers.append({
            "name":     "nous_portal",
            "base_url": "https://inference-api.nousresearch.com/v1",
            "model":    os.environ.get("NOUS_PORTAL_MODEL", "deepseek/deepseek-v4-flash"),
            "keys":     nous_portal_keys,
            "cost":     1,
        })

    openai_keys = _keys_for("openai", "OPENAI_API_KEYS")
    if openai_keys:
        providers.append({
            "name":     "openai",
            "base_url": "https://api.openai.com/v1",
            "model":    os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            "keys":     openai_keys,
            "cost":     1,
        })

    anthropic_keys = _keys_for("anthropic", "ANTHROPIC_API_KEYS")
    if anthropic_keys:
        providers.append({
            "name":     "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "model":    os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            "keys":     anthropic_keys,
            "cost":     1,
            "protocol": "anthropic",   # triggers format translation in forward()
        })

    # ════════════════════════════════════════════════════════════
    # Tier 4 — Local (no internet needed)
    # ════════════════════════════════════════════════════════════

    if os.environ.get("OLLAMA_ENABLED", "true").lower() != "false":
        providers.append({
            "name":     "ollama",
            "base_url": "http://localhost:11434/v1",
            "model":    os.environ.get("OLLAMA_MODEL", "qwen3.5:9b-16k"),
            "keys":     ["local"],
            "cost":     3,
        })

    # ════════════════════════════════════════════════════════════
    # Tier 5 — Tiny Context (only catch requests <6K tokens)
    # ════════════════════════════════════════════════════════════

    groq_keys = _keys_for("groq", "GROQ_API_KEYS")
    if groq_keys:
        providers.append({
            "name":     "groq",
            "base_url": "https://api.groq.com/openai/v1",
            "model":    os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
            "keys":     groq_keys,
            "cost":     0,
        })

    github_keys = _keys_for("github_models", "GITHUB_MODELS_TOKENS")
    if github_keys:
        providers.append({
            "name":     "github_models",
            "base_url": "https://models.inference.ai.azure.com",
            "model":    os.environ.get("GITHUB_MODELS_MODEL", "gpt-4o"),
            "keys":     github_keys,
            "cost":     0,
        })

    # ════════════════════════════════════════════════════════════
    # Tier 6 — Rarely hits (last resort / rarely useful)
    # ════════════════════════════════════════════════════════════

    gemini_keys = _keys_for("gemini", "GEMINI_API_KEYS")
    if gemini_keys:
        providers.append({
            "name":     "gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model":    os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite"),
            "keys":     gemini_keys,
            "cost":     0,
        })

    openrouter_keys = _keys_for("openrouter", "OPENROUTER_API_KEYS")
    if openrouter_keys:
        providers.append({
            "name":     "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
            "keys":     openrouter_keys,
            "cost":     0,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME", "cascade"),
            },
        })

    # --- deepseek-v4-flash ($0.098/$0.196 — daily driver, cheapest paid) ---
    if openrouter_keys:
        providers.append({
            "name":     "deepseek-v4-flash",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "deepseek/deepseek-v4-flash",
            "keys":     openrouter_keys,
            "cost":     1,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- glm-5.2 (OpenRouter → GLM-5.2 at $0.93/$3.00/M tokens) ---
    if openrouter_keys:
        providers.append({
            "name":     "glm-5.2",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "z-ai/glm-5.2",
            "keys":     openrouter_keys,
            "cost":     2,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- deepseek-v4-pro ($0.435/$0.87 — frontier, step up from Flash) ---
    if openrouter_keys:
        providers.append({
            "name":     "deepseek-v4-pro",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "deepseek/deepseek-v4-pro",
            "keys":     openrouter_keys,
            "cost":     2,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- sonnet-5 ($2/$10 — frontier reasoning/coding) ---
    if openrouter_keys:
        providers.append({
            "name":     "sonnet-5",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "anthropic/claude-sonnet-5",
            "keys":     openrouter_keys,
            "cost":     2,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- nemotron-ultra-free (NVIDIA Nemotron 3 Ultra 550B, FREE — 1M context, reasoning) ---
    if openrouter_keys:
        providers.append({
            "name":     "nemotron-ultra-free",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "nvidia/nemotron-3-ultra-550b-a55b:free",
            "keys":     openrouter_keys,
            "cost":     0,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- sonnet-4.6 ($3/$15 — frontier coding/reasoning, 1M context) ---
    if openrouter_keys:
        providers.append({
            "name":     "sonnet-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "anthropic/claude-sonnet-4.6",
            "keys":     openrouter_keys,
            "cost":     2,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- mimo-v2.5 ($0.105/$0.28 — cheap omnimodal, 1M context) ---
    if openrouter_keys:
        providers.append({
            "name":     "mimo-v2.5",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "xiaomi/mimo-v2.5",
            "keys":     openrouter_keys,
            "cost":     1,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- hy3-preview ($0.063/$0.21 — cheapest reasoning/agent model) ---
    if openrouter_keys:
        providers.append({
            "name":     "hy3-preview",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "tencent/hy3-preview",
            "keys":     openrouter_keys,
            "cost":     1,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    # --- minimax-m3 ($0.30/$1.20 — 1M context multimodal) ---
    if openrouter_keys:
        providers.append({
            "name":     "minimax-m3",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    "minimax/minimax-m3",
            "keys":     openrouter_keys,
            "cost":     1,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL",
                    "https://github.com/chrisluersen/cascade"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME",
                    "cascade"),
            },
        })

    zai_keys = _keys_for("zai", "GLM_API_KEYS")
    if zai_keys:
        providers.append({
            "name":     "zai",
            "base_url": "https://api.z.ai/api/paas/v4",
            "model":    os.environ.get("ZAI_MODEL", "glm-4.5-flash"),
            "keys":     zai_keys,
            "cost":     0,
        })

    naga_keys = _keys_for("naga", "NAGA_API_KEYS")
    if naga_keys:
        providers.append({
            "name":     "naga",
            "base_url": "https://api.naga.ac/v1",
            "model":    os.environ.get("NAGA_MODEL", "nemotron-3-super-120b-a12b:free"),
            "keys":     naga_keys,
            "cost":     0,
        })

    huggingface_keys = _keys_for("huggingface", "HUGGINGFACE_API_KEYS")
    if huggingface_keys:
        providers.append({
            "name":     "huggingface",
            "base_url": "https://router.huggingface.co/v1",
            "model":    os.environ.get("HUGGINGFACE_MODEL", "openai/gpt-oss-120b:cheapest"),
            "keys":     huggingface_keys,
            "cost":     0,
        })

    # --- deepinfra (free, Qwen2.5-72B-Instruct) ---
    deepinfra_keys = _keys_for("deepinfra", "DEEPINFRA_API_KEYS")
    if deepinfra_keys:
        providers.append({
            "name":     "deepinfra",
            "base_url": "https://api.deepinfra.com/v1/openai",
            "model":    os.environ.get("DEEPINFRA_MODEL", "Qwen/Qwen2.5-72B-Instruct"),
            "keys":     deepinfra_keys,
            "cost":     0,
        })

    # --- fireworks (free, Qwen2.5-Coder-32B-Instruct) ---
    fireworks_keys = _keys_for("fireworks", "FIREWORKS_API_KEYS")
    if fireworks_keys:
        providers.append({
            "name":     "fireworks",
            "base_url": "https://api.fireworks.ai/inference/v1",
            "model":    os.environ.get("FIREWORKS_MODEL", "accounts/fireworks/models/qwen2p5-coder-32b-instruct"),
            "keys":     fireworks_keys,
            "cost":     0,
        })

    # ── Together AI (paid — free tier discontinued, Qwen3.5-9B ~$0.02/M) ──
    together_keys = _keys_for("together", "TOGETHER_API_KEY")
    if together_keys:
        providers.append({
            "name":     "together",
            "base_url": "https://api.together.xyz/v1",
            "model":    os.environ.get("TOGETHER_MODEL", "Qwen/Qwen3.5-9B"),
            "keys":     together_keys,
            "cost":     1,
        })

    # ── LLM7.io (paid — free tier discontinued; devstral-small-2:24b ~lowest cost) ──
    llm7_keys = _keys_for("llm7", "LLM7_API_KEY")
    if llm7_keys:
        providers.append({
            "name":     "llm7",
            "base_url": "https://api.llm7.io/v1",
            "model":    os.environ.get("LLM7_MODEL", "devstral-small-2:24b"),
            "keys":     llm7_keys,
            "cost":     1,
        })

    # ── OVHcloud AI Endpoints (free anonymous tier, no signup, no key) ─────
    # 2 RPM per IP per model on anonymous tier. No key, no registration needed.
    # Models: Qwen3.5-397B-A17B, gpt-oss-120b, Qwen3.6-27B, DeepSeek-R1, etc.
    # Sign up for a Public Cloud project for $200 free credits + 400 RPM.
    ovh_keys = _keys_for("ovhcloud", "OVH_API_KEY")  # leave unset for anonymous tier
    providers.append({
        "name":     "ovhcloud",
        "base_url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        "model":    os.environ.get("OVHCLOUD_MODEL", "gpt-oss-120b"),
        "keys":     ovh_keys or [""],
        "cost":     0,
    })

    # ── Aion Labs (permanent free, no credit card — 15 RPM, 20K TPD) ───
    aion_keys = _keys_for("aion", "AION_API_KEY")
    if aion_keys:
        providers.append({
            "name":     "aion",
            "base_url": "https://api.aionlabs.ai/v1",
            "model":    os.environ.get("AION_MODEL", "gpt-4o-mini"),
            "keys":     aion_keys,
            "cost":     0,
        })

    # ── LongCat (permanent free tier — 55M tokens/day) ──────────────────
    longcat_keys = _keys_for("longcat", "LONGCAT_API_KEY")
    if longcat_keys:
        providers.append({
            "name":     "longcat",
            "base_url": "https://api.longcat.chat/openai/v1",
            "model":    os.environ.get("LONGCAT_MODEL", "LongCat-2.0"),
            "keys":     longcat_keys,
            "cost":     0,
        })

    # ── SiliconFlow (free — DeepSeek-V2.5, 30 RPM, 60K TPM) ────────────────────
    siliconflow_keys = _keys_for("siliconflow", "SILICONFLOW_API_KEY")
    if siliconflow_keys:
        providers.append({
            "name":     "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "model":    os.environ.get("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V2.5"),
            "keys":     siliconflow_keys,
            "cost":     0,
        })

    # ── AI Hub Mix (free tier — 2 RPM, 100K context) ─────────────────────
    aihubmix_keys = _keys_for("aihubmix", "AIHUBMIX_API_KEY")
    if aihubmix_keys:
        providers.append({
            "name":     "aihubmix",
            "base_url": "https://api.aihubmix.com/v1",
            "model":    os.environ.get("AIHUBMIX_MODEL", "coding-glm-5.2-free"),
            "keys":     aihubmix_keys,
            "cost":     0,
        })

    if not providers:
        log.warning("No providers configured — set GEMINI_API_KEYS, OPENROUTER_API_KEYS, etc. in .env")

    # Per-provider "skip when the request is too big" ceiling. Some free tiers
    # reject large payloads outright, so trying them with a big prompt just wastes

    # a round-trip before cascading. When the estimated request size exceeds a
    # provider's ceiling, that provider is skipped entirely.
    #   Configure via  {PROVIDER}_SKIP_TOKENS_OVER  (0 = never skip).
    # Defaults match each free tier's known limit:
    #   • groq          ~6000 TPM → 413
    #   • sambanova     DeepSeek-V3.2 here caps at 32K context → 400
    #   • github_models gpt-4o free tier ~8K input-token limit → 413
    _skip_defaults = {"groq": 5500, "sambanova": 30000, "github_models": 6000}
    for p in providers:
        env_var = f"{p['name'].upper()}_SKIP_TOKENS_OVER"
        p["skip_if_tokens_over"] = _int_env(env_var, _skip_defaults.get(p["name"], 0))

    # Per-provider output-token ceiling. Some providers 400 the whole request when
    # max_tokens exceeds their output cap, so we clamp it down in forward().
    #   Configure via  {PROVIDER}_MAX_OUTPUT_TOKENS  (0 = no clamp).
    #   • cohere        command-a caps output at 8192
    #   • github_models gpt-4o here rejects very large max_tokens (e.g. 65536)
    _max_out_defaults = {"cohere": 8192, "github_models": 16384, "groq": 32768}
    for p in providers:
        env_var = f"{p['name'].upper()}_MAX_OUTPUT_TOKENS"
        p["max_output_tokens"] = _int_env(env_var, _max_out_defaults.get(p["name"], 0))

    # Per-provider embedding model. Only providers with a non-empty embed model
    # take part in /v1/embeddings routing (OpenRouter, Groq, etc. are chat-only).
    # Each uses the same base_url with an /embeddings path; the wire format is
    # OpenAI-compatible, so no translation is needed. Configure or enable more
    # via {PROVIDER}_EMBED_MODEL (empty string disables a provider for embeds).
    # NVIDIA is intentionally omitted: its embedding models are "asymmetric" and
    # require an input_type (query/passage) parameter that the OpenAI embeddings
    # format doesn't carry, so they can't be served by clean passthrough. Enable
    # one explicitly with NVIDIA_EMBED_MODEL if you know it accepts OpenAI format.
    _embed_defaults = {
        "gemini":  "gemini-embedding-001",
        "mistral": "mistral-embed",
        "openai":  "text-embedding-3-small",
        "cohere":  "embed-v4.0",
    }
    for p in providers:
        env_var = f"{p['name'].upper()}_EMBED_MODEL"
        p["embed_model"] = os.environ.get(env_var, _embed_defaults.get(p["name"], ""))

    return providers


PROVIDERS = _build_providers()

# Providers whose /models endpoint mixes paid models in with the free ones.
# When auto-discovering a replacement model for these, restrict to :free ids so
# a probe can never silently promote cascade onto a paid model.
_FREE_ONLY_DISCOVERY = {"openrouter", "naga"}

# ── Credential pool ────────────────────────────────────────────────────────────

# ── Smart routing helpers ─────────────────────────────────────────────────────

def _rate_model(model_name: str) -> int:
    mn = model_name.lower()
    for key in sorted(KNOWN_MODEL_RATINGS, key=len, reverse=True):
        if key in mn:
            return KNOWN_MODEL_RATINGS[key]
    for rating, patterns in _RATING_PATTERNS:
        if any(p in mn for p in patterns):
            return rating
    return 3


def _discover_best_model(base_url: str, key: str, extra_headers: dict = None,
                         free_only: bool = False) -> str | None:
    try:
        hdrs = {"Authorization": f"Bearer {key}", **(extra_headers or {})}
        r = _HTTP.get(f"{base_url.rstrip('/')}/models", headers=hdrs, timeout=10)
        if r.status_code != 200:
            return None
        models = [m["id"] for m in r.json().get("data", []) if isinstance(m.get("id"), str)]
        if free_only:
            models = [m for m in models if m.endswith(":free")]
        return min(models, key=_rate_model) if models else None
    except Exception:
        return None


def _probe_anthropic(provider: dict, key: str) -> tuple:
    """Probe Anthropic using the Messages API (not OpenAI-format /chat/completions)."""
    url  = "https://api.anthropic.com/v1/messages"
    hdrs = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    body = {"model": provider["model"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    t0 = time.time()
    try:
        r = _HTTP.post(url, headers=hdrs, json=body, timeout=12)
        latency = (time.time() - t0) * 1000
        return r.status_code == 200, latency, provider["model"]
    except requests.exceptions.ReadTimeout:
        return True, (time.time() - t0) * 1000, provider["model"]
    except Exception:
        return False, (time.time() - t0) * 1000, provider["model"]


def _probe_provider(provider: dict, key: str) -> tuple:
    """Returns (success, latency_ms, model_used). Auto-discovers alt model on 400/404.

    A read-timeout means the provider accepted the request and is still
    generating — alive but slow. Large MoE models can cold-start for 30–60s,
    past the probe window, so a read-timeout counts as available rather than
    wrongly dropping a working provider to the back of its rating tier. Only a
    connection failure (host unreachable) counts as down."""
    if provider.get("protocol") == "anthropic":
        return _probe_anthropic(provider, key)

    url  = provider["base_url"].rstrip("/") + "/chat/completions"
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
            **provider.get("headers", {})}
    body = {"model": provider["model"],
            "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    t0 = time.time()
    try:
        r = _HTTP.post(url, headers=hdrs, json=body, timeout=12)
        latency = (time.time() - t0) * 1000
        if r.status_code == 200:
            return True, latency, provider["model"]
        if r.status_code in (400, 404):
            # Providers that list paid models alongside free ones — never let
            # auto-discovery silently pick something that costs credits.
            alt = _discover_best_model(provider["base_url"], key, provider.get("headers", {}),
                                       free_only=provider["name"] in _FREE_ONLY_DISCOVERY)
            if alt:
                body["model"] = alt
                t0 = time.time()
                r2 = _HTTP.post(url, headers=hdrs, json=body, timeout=12)
                if r2.status_code == 200:
                    return True, (time.time() - t0) * 1000, alt
        return False, (time.time() - t0) * 1000, provider["model"]
    except requests.exceptions.ReadTimeout:
        # Connected, still generating — alive, just slow (cold MoE start).
        return True, (time.time() - t0) * 1000, provider["model"]
    except Exception:
        return False, (time.time() - t0) * 1000, provider["model"]


_TOOL_PROBE = [{"type": "function", "function": {
    "name": "get_weather", "description": "Get the current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]}}}]


def _probe_tools(provider: dict, key: str, model: str) -> bool:
    """Detect whether a provider's model supports function calling. Sends a tiny
    request that forces a tool call (tool_choice=required, falling back to auto
    for providers that reject 'required') and checks whether the model actually
    emits one. Anthropic providers always support tools."""
    if provider.get("protocol") == "anthropic":
        return True
    url  = provider["base_url"].rstrip("/") + "/chat/completions"
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
            **provider.get("headers", {})}
    base = {"model": model, "max_tokens": 64, "tools": _TOOL_PROBE,
            "messages": [{"role": "user", "content": "What is the weather in Paris? Use the get_weather tool."}]}
    for choice in ("required", "auto"):
        try:
            r = _HTTP.post(url, headers=hdrs, json={**base, "tool_choice": choice}, timeout=12)
        except Exception:
            return False
        if r.status_code != 200:
            continue   # provider may reject tool_choice=required → try auto
        try:
            msg = (r.json().get("choices") or [{}])[0].get("message") or {}
            if msg.get("tool_calls"):
                return True
        except Exception:
            return False
    return False


def _probe_reasoning(provider: dict, key: str, model: str) -> bool:
    """Detect whether a provider's model is a 'reasoning' model — one that spends
    output tokens on hidden chain-of-thought before answering. These return empty
    content if max_tokens is too small to cover the thinking. We probe with a
    small budget and a trivial prompt: a reasoning model exposes a reasoning field
    or burns the whole budget thinking (empty content, truncated), while a normal
    model just answers. Anthropic's thinking is opt-in, so it's treated as normal."""
    if provider.get("protocol") == "anthropic":
        return False
    url  = provider["base_url"].rstrip("/") + "/chat/completions"
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
            **provider.get("headers", {})}
    body = {"model": model, "max_tokens": 24,
            "messages": [{"role": "user", "content": "Reply with just the word: ready"}]}
    try:
        r = _HTTP.post(url, headers=hdrs, json=body, timeout=12)
        if r.status_code != 200:
            return False
        choice = (r.json().get("choices") or [{}])[0]
        msg     = choice.get("message") or {}
        content = (msg.get("content") or "").strip()
        if msg.get("reasoning_content") or msg.get("reasoning"):
            return True
        return not content and choice.get("finish_reason") == "length"
    except Exception:
        return False


def classify_complexity(messages: list) -> int:
    """Heuristic: 1 (critical) → 5 (trivial). No LLM call."""
    content = " ".join(
        m["content"] if isinstance(m.get("content"), str)
        else " ".join(p.get("text", "") for p in m["content"] if isinstance(p, dict))
        for m in messages if m.get("content")
    )
    tokens = len(content) // 4
    cl = content.lower()
    has_code    = "```" in content or any(k in cl for k in ["def ", "function ", "class ", "import "])
    has_complex = any(k in cl for k in ["implement", "design", "architect", "debug", "refactor",
                                         "algorithm", "optimize", "analyze", "build", "develop",
                                         "summarize", "explain how", "compare", "research", "create a plan",
                                         "generate", "convert", "migrate", "write tests", "test cases",
                                         "step by step", "walk me through", "help me understand"])
    has_simple  = any(k in cl for k in ["what is", "who is", "define", "translate", "yes or no",
                                         "how many", "give me a number", "true or false", "in one word",
                                         "spell", "what does", "one sentence", "yes or no answer",
                                         "what year", "what time", "how old"])
    if tokens > 2000 or (has_code and has_complex): return 1
    if tokens > 800  or has_complex:                return 2
    if tokens > 300  or has_code:                   return 3
    if tokens > 100  or (not has_simple):           return 4
    return 5


def _get_smart_ordered(providers: list, complexity: int, est_tokens: int = 0) -> list:
    """
    Sort providers for this complexity: cheapest capable model first, then
    overkill models, then too-weak as last resort. Never blocks.

    When FAST_ROUTE_THRESHOLD is set and the request is shorter than it,
    low-latency providers win ties between otherwise equally-ranked options.

    Round-robin: providers that tie on every criterion (same rating, same
    availability) are rotated each request so load spreads across them instead
    of always hitting the same one first. We rotate the list by a per-request
    counter before sorting; the sort is stable, so equal-keyed providers keep
    their (rotated) relative order.
    """
    fast_first = FAST_ROUTE_TOKENS > 0 and 0 < est_tokens < FAST_ROUTE_TOKENS

    def _key(p):
        state  = _provider_state.get(p["name"], {})
        rating = state.get("rating", _rate_model(p["model"]))
        avail  = state.get("available", True)
        fast   = 0 if (fast_first and p["name"] in _FAST_PROVIDERS) else 1
        cost   = p.get("cost", 0)
        lat    = state.get("latency_ms", 99999)

        # Reasoning support: for complex tasks (complexity 1-2), prefer
        # providers whose model actually supports chain-of-thought thinking.
        # Gets model with better quality answers for hard problems.
        needs_reasoning = 1 if complexity <= 2 else 0
        reasoning_boost = 0 if (needs_reasoning and state.get("reasoning")) else 1

        # Actual per-1M-token cost from KNOWN_MODEL_COSTS — within the same
        # cost tier, cheaper providers should be tried first so we don't waste
        # money on expensive models when a cheaper one is just as good.
        model_name = state.get("model", p.get("model", ""))
        _cost_pair = KNOWN_MODEL_COSTS.get(model_name, (0.0, 0.0))
        token_cost = _cost_pair[0] + _cost_pair[1]  # input + output per 1M

        # Health-aware terms — tier/sort_within stay FIRST so capability matching
        # is never overridden by health (a healthy weak model must not outrank the
        # correct-capability one). When every candidate is healthy these two terms
        # are constant (0), leaving the existing tie order untouched.
        breaker_open = 1 if stats.breaker_open(p["name"]) else 0  # open breakers sink within tier
        health       = stats.health_bucket(p["name"])            # 0 healthy / 1 degraded / 2 bad
        if rating <= complexity:
            tier        = 0
            sort_within = complexity - rating   # 0 = perfect match, larger = overkill
        else:
            tier        = 1
            sort_within = rating - complexity   # too weak — closest first
        return (cost, tier, sort_within,
                breaker_open, health,
                reasoning_boost,          # 0 = reasoning-capable AND complex task (promoted)
                0 if avail else 1,         # available first
                lat,                       # faster first
                token_cost,                # cheaper per-token first
                fast)

    n = len(providers)
    offset = next(_rr_counter) % n if n else 0
    rotated = providers[offset:] + providers[:offset]
    return sorted(rotated, key=_key)


def _initialize_ratings(providers: list, pool_ref):
    """Background: probe all providers, fix bad models, assign ratings, persist state."""
    global _provider_state
    if STATE_FILE.exists():
        try:
            cached_doc = json.loads(STATE_FILE.read_text())
            _provider_state = cached_doc.get("providers", {})
            log.info(f"[ratings] Loaded cached state ({len(_provider_state)} providers)")
            # Probes cost a real completion per provider, so skip them while the
            # state is fresh and still covers every configured provider.
            age = time.time() - cached_doc.get("last_updated_ts", 0)
            if (STATE_TTL_HOURS > 0 and age < STATE_TTL_HOURS * 3600
                    and all(p["name"] in _provider_state for p in providers)):
                for p in providers:
                    cached_model = _provider_state[p["name"]].get("model")
                    if cached_model:
                        p["model"] = cached_model
                log.info(f"[ratings] State is {age/3600:.1f}h old (< {STATE_TTL_HOURS}h TTL) "
                         "— skipping startup probes")
                return
        except Exception:
            pass

    log.info("[ratings] Background provider validation starting…")
    new_state = {}
    for p in providers:
        name  = p["name"]
        probe = pool_ref.pools.get(name, [])
        if not probe:
            new_state[name] = {"rating": _rate_model(p["model"]), "model": p["model"],
                                "available": False, "latency_ms": 0, "overridden": False}
            continue
        key = probe[0]["key"]
        ok, latency, actual = _probe_provider(p, key)
        original   = p["model"]
        overridden = actual != original
        if overridden:
            log.info(f"[ratings]   {name}: model fixed {original} → {actual}")
            p["model"] = actual
        rating = _rate_model(actual)
        # Tool-capability: an explicit env override wins; otherwise probe (only
        # when reachable — no point asking a down provider).
        env_tools = os.environ.get(f"{name.upper()}_SUPPORTS_TOOLS")
        if env_tools is not None:
            supports_tools = env_tools.strip().lower() not in ("0", "false", "no", "")
        elif ok:
            supports_tools = _probe_tools(p, key, actual)
        else:
            supports_tools = False
        # Reasoning-model detection (env override wins, else probe when reachable).
        env_reason = os.environ.get(f"{name.upper()}_REASONING")
        if env_reason is not None:
            reasoning = env_reason.strip().lower() not in ("0", "false", "no", "")
        elif ok:
            reasoning = _probe_reasoning(p, key, actual)
        else:
            reasoning = False
        log.info(f"[ratings]   {name}: {'✓' if ok else '✗'} rating={rating} model={actual} "
                 f"{latency:.0f}ms tools={'yes' if supports_tools else 'no'} "
                 f"reasoning={'yes' if reasoning else 'no'}")
        new_state[name] = {"rating": rating, "model": actual, "available": ok,
                            "latency_ms": round(latency, 1), "overridden": overridden,
                            "original_model": original, "supports_tools": supports_tools,
                            "reasoning": reasoning}
    _provider_state = new_state
    try:
        STATE_FILE.write_text(json.dumps({"last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                           "last_updated_ts": time.time(),
                                           "providers": new_state}, indent=2))
        log.info("[ratings] State persisted to disk")
    except Exception as e:
        log.warning(f"[ratings] Could not persist state: {e}")


class CredentialPool:
    """Thread-safe round-robin key pool with per-key cooldown tracking."""

    def __init__(self, providers: list[dict]):
        self.lock  = threading.Lock()
        self.pools: dict[str, deque] = {}
        for p in providers:
            self.pools[p["name"]] = deque(
                {"key": k, "cool_until": 0.0} for k in p["keys"]
            )
            log.info(f"  {p['name']}: {len(p['keys'])} key(s) loaded")

    def get_key(self, provider_name: str) -> str | None:
        """Return the next ready key (round-robin), or None if all are cooling."""
        with self.lock:
            pool = self.pools.get(provider_name, deque())
            now  = time.time()
            for _ in range(len(pool)):
                entry = pool[0]
                pool.rotate(-1)
                if entry["cool_until"] <= now:
                    return entry["key"]
            return None

    def mark_rate_limited(self, provider_name: str, key: str, retry_after: int = 60):
        """Put a specific key into cooldown."""
        with self.lock:
            for entry in self.pools.get(provider_name, []):
                if entry["key"] == key:
                    entry["cool_until"] = time.time() + retry_after
                    log.warning(f"  {provider_name} key ...{key[-6:]} cooling for {retry_after}s")
                    return


pool = CredentialPool(PROVIDERS)

# ── Bulkhead manager ────────────────────────────────────────────────────────────
class BulkheadManager:
    """Per-provider concurrency limiter. Prevents a single slow provider from
    consuming all worker threads. Uses a lock-protected counter (not Semaphore)
    so we can inspect active counts for observability."""
    def __init__(self, providers: list[dict], max_concurrent: int = 4):
        self.max = max_concurrent
        self.lock = threading.Lock()
        self._active: dict[str, int] = {}
        for p in providers:
            self._active[p["name"]] = 0

    def try_acquire(self, provider_name: str) -> bool:
        """Try to start a request. Returns True if under limit, False if capped."""
        if self.max <= 0:
            return True
        with self.lock:
            c = self._active.get(provider_name, 0)
            if c >= self.max:
                return False
            self._active[provider_name] = c + 1
            return True

    def release(self, provider_name: str):
        if self.max <= 0:
            return
        with self.lock:
            c = self._active.get(provider_name, 0)
            if c > 0:
                self._active[provider_name] = c - 1

    def status(self) -> dict[str, dict]:
        with self.lock:
            return {n: {"max": self.max if self.max > 0 else 0,
                         "active": c}
                    for n, c in self._active.items()}

bulkhead = BulkheadManager(PROVIDERS, BULKHEAD_MAX)

# Background: validate providers, fix models, assign ratings
threading.Thread(target=_initialize_ratings, args=(PROVIDERS, pool), daemon=True).start()

# ── Per-provider stats ─────────────────────────────────────────────────────────

class ProviderStats:
    """Tracks latency and error rates per provider for observability."""

    def __init__(self):
        self.lock   = threading.Lock()
        self._data: dict[str, dict] = {}

    def _ensure(self, name: str):
        if name not in self._data:
            self._data[name] = {"latency_sum": 0.0, "latency_count": 0,
                                "error_count": 0, "request_count": 0,
                                "health": deque(maxlen=BREAKER_WINDOW), "open_until": 0.0,
                                "cost_total": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
                                "trace_ids": deque(maxlen=100)}

    def record_success(self, name: str, latency_s: float):
        with self.lock:
            self._ensure(name)
            s = self._data[name]
            s["latency_sum"]   += latency_s
            s["latency_count"] += 1
            s["request_count"] += 1

    def record_error(self, name: str):
        with self.lock:
            self._ensure(name)
            s = self._data[name]
            s["error_count"]   += 1
            s["request_count"] += 1

    def record_cost(self, name: str, cost_usd: float, prompt_tok: int, completion_tok: int):
        with self.lock:
            self._ensure(name)
            s = self._data[name]
            s["cost_total"] += cost_usd
            s["prompt_tokens"] += prompt_tok
            s["completion_tokens"] += completion_tok

    def record_trace(self, name: str, trace_id: str):
        with self.lock:
            self._ensure(name)
            self._data[name]["trace_ids"].append(trace_id)

    # ── Circuit breaker ──────────────────────────────────────────────────────
    def record_health(self, name: str, ok: bool):
        """Record a HEALTH outcome (separate from request stats — breaker only).
        On failure: trip the breaker open once the window has enough samples and
        the health-fail fraction crosses the threshold. On success: half-open
        recovery — close the breaker and wipe the window for a clean slate."""
        with self.lock:
            self._ensure(name)
            s   = self._data[name]
            win = s["health"]
            win.append(ok)
            if ok:
                s["open_until"] = 0.0
                win.clear()
            elif len(win) >= BREAKER_MIN_SAMPLES:
                fails = sum(1 for x in win if not x)
                if fails / len(win) >= BREAKER_ERROR_RATE:
                    s["open_until"] = time.time() + BREAKER_COOLDOWN

    def breaker_open(self, name: str) -> bool:
        with self.lock:
            s = self._data.get(name)
            return bool(s) and time.time() < s.get("open_until", 0.0)

    def breaker_status(self, name: str) -> dict:
        with self.lock:
            s   = self._data.get(name, {})
            now = time.time()
            open_until = s.get("open_until", 0.0)
            win   = s.get("health", ())
            fails = sum(1 for x in win if not x)
            return {"open": now < open_until,
                    "opens_in_s": max(0, round(open_until - now)),
                    "recent_health_fails": fails}

    def health_bucket(self, name: str) -> int:
        """Recent error-rate bucket for routing: 0 healthy / 1 degraded / 2 bad.
        Too few samples → 0 (unknown = healthy; don't penalize new providers)."""
        with self.lock:
            s = self._data.get(name)
            if not s:
                return 0
            win = s.get("health", ())
            if len(win) < BREAKER_MIN_SAMPLES:
                return 0
            err_rate = sum(1 for x in win if not x) / len(win)
            return 0 if err_rate < 0.10 else (1 if err_rate < 0.50 else 2)

    def summary(self, name: str) -> dict:
        with self.lock:
            s  = self._data.get(name, {})
            lc = s.get("latency_count", 0)
            rc = s.get("request_count", 0)
            ec = s.get("error_count", 0)
            return {
                "avg_latency_ms": round(s.get("latency_sum", 0) / lc * 1000) if lc else None,
                "error_rate":     round(ec / rc, 3) if rc else 0.0,
                "total_requests": rc,
                "errors":         ec,
                "cost_total_usd": round(s.get("cost_total", 0.0), 6),
                "prompt_tokens":  s.get("prompt_tokens", 0),
                "completion_tokens": s.get("completion_tokens", 0),
            }

    def all_summaries(self) -> dict:
        with self.lock:
            return {name: self.summary(name) for name in self._data}


stats = ProviderStats()

# ── Response cache ─────────────────────────────────────────────────────────────

class ResponseCache:
    """
    In-memory LRU cache for non-streaming responses.
    Identical requests (same model + messages) return a cached copy,
    saving free-tier quota for novel queries.
    Set CACHE_TTL_SECONDS=0 to disable.
    """

    def __init__(self, ttl: int = 300, max_size: int = 100):
        self.ttl      = ttl
        self.max_size = max_size
        self.lock     = threading.Lock()
        self._store: OrderedDict = OrderedDict()  # hash -> (data, timestamp)
        self.hits     = 0
        self.misses   = 0

    def _hash(self, payload: dict) -> str:
        # Hash the entire request (minus "stream", which doesn't change the
        # answer) so requests differing only in temperature, max_tokens,
        # tools, response_format, etc. never collide.
        relevant = {k: v for k, v in payload.items() if k != "stream"}
        content = json.dumps(relevant, sort_keys=True, default=str)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def get(self, payload: dict) -> dict | None:
        if self.ttl <= 0:
            return None
        key = self._hash(payload)
        with self.lock:
            if key in self._store:
                data, ts = self._store[key]
                if time.time() - ts < self.ttl:
                    self._store.move_to_end(key)
                    self.hits += 1
                    return data
                del self._store[key]
            self.misses += 1
        return None

    def set(self, payload: dict, data: dict):
        if self.ttl <= 0:
            return
        key = self._hash(payload)
        with self.lock:
            if len(self._store) >= self.max_size:
                self._store.popitem(last=False)  # evict oldest
            self._store[key] = (data, time.time())

    @property
    def size(self) -> int:
        with self.lock:
            return len(self._store)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 3) if total else 0.0


cache = ResponseCache(ttl=CACHE_TTL, max_size=CACHE_MAX_SIZE)

# ── Thinking field stripping ───────────────────────────────────────────────────
# Some providers (e.g. Gemini 2.5) emit reasoning/thinking fields in responses.
# These fields cause 400 errors on other providers (Groq, Cerebras, OpenRouter).
# We strip them from both outgoing requests and incoming responses.

def _strip_message(msg: dict):
    """Remove thinking fields and provider-unsupported metadata from a message dict in-place."""
    msg.pop("reasoning_content", None)
    msg.pop("reasoning", None)
    msg.pop("think", None)
    msg.pop("timestamp", None)   # Hermes Agent metadata; Cerebras rejects it
    if isinstance(msg.get("content"), list):
        msg["content"] = [
            b for b in msg["content"]
            if b.get("type") not in ("thinking", "think")
        ]


def _strip_response(data: dict):
    """Strip thinking fields from a non-streaming response before returning it."""
    for choice in data.get("choices", []):
        if "message" in choice:
            _strip_message(choice["message"])


def _streaming_generator(resp: requests.Response):
    """
    Yield SSE chunks with thinking fields stripped from delta objects.
    Buffers by newline to handle chunks that split across SSE boundaries.
    """
    buf = b""
    for raw_chunk in resp.iter_content(chunk_size=None):
        buf += raw_chunk
        while b"\n" in buf:
            line_bytes, buf = buf.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace")
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    event = _replace_surrogates(json.loads(line[6:]))
                    for choice in event.get("choices", []):
                        delta = choice.get("delta", {})
                        delta.pop("reasoning_content", None)
                        delta.pop("reasoning", None)
                        delta.pop("think", None)
                    yield ("data: " + json.dumps(event) + "\n").encode("utf-8")
                    continue
                except (json.JSONDecodeError, Exception):
                    pass
            yield (line + "\n").encode("utf-8")
    if buf:
        yield buf

# ── Anthropic format translation ──────────────────────────────────────────────
# Anthropic's Messages API uses a different format from OpenAI. These helpers
# translate transparently so the caller never has to know which provider they hit.

def _to_anthropic_body(payload: dict, model: str) -> dict:
    """Convert an OpenAI chat-completions request body to Anthropic Messages format."""
    system_parts = []
    messages = []
    for msg in payload.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")
        # Flatten list content to plain text
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
        if role == "system":
            system_parts.append(content)
        else:
            # Merge consecutive same-role messages (Anthropic requires alternating roles)
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n" + content
            else:
                messages.append({"role": role, "content": content})

    body: dict = {
        "model":      model,
        "messages":   messages,
        "max_tokens": payload.get("max_tokens") or 1024,
    }
    if system_parts:
        system_text = "\n".join(system_parts)
        # Anthropic prompt caching: mark system prompt for caching when it's long
        # enough to qualify (≥ 1024 tokens; estimated as ≥ 4096 chars). Cached
        # tokens are billed at 10% on subsequent requests — transparent to the caller.
        if len(system_text) >= 4096:
            body["system"] = [{"type": "text", "text": system_text,
                                "cache_control": {"type": "ephemeral"}}]
        else:
            body["system"] = system_text
    if payload.get("stream"):
        body["stream"] = True
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    stop = payload.get("stop")
    if stop:
        body["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    return body


def _from_anthropic_response(data: dict) -> dict:
    """Convert an Anthropic Messages response to OpenAI chat-completion format."""
    content = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    )
    stop_reason = data.get("stop_reason", "end_turn")
    finish_reason = "stop" if stop_reason in ("end_turn", "stop_sequence") else "length"
    usage = data.get("usage", {})
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    out: dict = {
        "id":      data.get("id", "msg_unknown"),
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   data.get("model", ""),
        "choices": [{
            "index":         0,
            "message":       {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens":     prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens":      prompt_tokens + completion_tokens,
        },
    }
    # Pass through Anthropic cache token counts when present so callers can
    # observe cache savings without breaking OpenAI-compatible clients.
    if usage.get("cache_read_input_tokens"):
        out["usage"]["cache_read_input_tokens"] = usage["cache_read_input_tokens"]
    if usage.get("cache_creation_input_tokens"):
        out["usage"]["cache_creation_input_tokens"] = usage["cache_creation_input_tokens"]
    return out


def _anthropic_streaming_generator(resp: requests.Response):
    """Translate Anthropic SSE stream to OpenAI SSE format token-by-token."""
    msg_id       = f"chatcmpl-{int(time.time())}"
    model        = ""
    created      = int(time.time())
    finish_reason = "stop"
    first_chunk  = True

    buf = b""
    for raw_chunk in resp.iter_content(chunk_size=None):
        buf += raw_chunk
        while b"\n" in buf:
            line_bytes, buf = buf.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            if etype == "message_start":
                msg    = event.get("message", {})
                msg_id = msg.get("id", msg_id)
                model  = msg.get("model", "")
                # Emit role chunk
                chunk = {"id": msg_id, "object": "chat.completion.chunk", "created": created,
                         "model": model,
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                      "finish_reason": None}]}
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
                first_chunk = False

            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text  = delta.get("text", "")
                    chunk = {"id": msg_id, "object": "chat.completion.chunk", "created": created,
                             "model": model,
                             "choices": [{"index": 0, "delta": {"content": text},
                                          "finish_reason": None}]}
                    yield ("data: " + json.dumps(chunk) + "\n\n").encode()

            elif etype == "message_delta":
                sr = event.get("delta", {}).get("stop_reason", "end_turn")
                finish_reason = "stop" if sr in ("end_turn", "stop_sequence") else "length"

            elif etype == "message_stop":
                chunk = {"id": msg_id, "object": "chat.completion.chunk", "created": created,
                         "model": model,
                         "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
                yield b"data: [DONE]\n\n"


# ── Anthropic INBOUND translation (accept the Anthropic SDK's /v1/messages) ───
# The mirror image of the helpers above: these let a client using the Anthropic
# SDK talk to cascade. An incoming Anthropic request is converted to OpenAI
# format, routed through the normal pipeline, and the response is converted back.

_OPENAI_TO_ANTHROPIC_STOP = {"stop": "end_turn", "length": "max_tokens",
                             "tool_calls": "tool_use", "content_filter": "end_turn"}


def _anthropic_request_to_openai(body: dict) -> dict:
    """Convert an Anthropic /v1/messages request into an OpenAI chat payload.
    The model is deliberately NOT preserved — cascade picks a model per
    provider — so an Anthropic-SDK client transparently gets multi-provider
    failover instead of being pinned to whatever model string it sent.

    Tool use is mapped both ways: Anthropic `tools`/`tool_choice`, assistant
    `tool_use` content blocks, and user `tool_result` blocks become the OpenAI
    equivalents (function tools, message `tool_calls`, and `role:"tool"`
    messages)."""
    messages = []
    system = body.get("system")
    if isinstance(system, list):   # Anthropic allows system as a list of text blocks
        system = "\n".join(b.get("text", "") for b in system
                           if isinstance(b, dict) and b.get("type") == "text")
    if system:
        messages.append({"role": "system", "content": system})

    for m in body.get("messages", []):
        role    = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        # List content: text / tool_use (assistant calls) / tool_result (user returns).
        text_parts, tool_calls, tool_msgs = [], [], []
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                text_parts.append(b.get("text", ""))
            elif bt == "tool_use":
                tool_calls.append({"id": b.get("id"), "type": "function",
                                   "function": {"name": b.get("name", ""),
                                                "arguments": json.dumps(b.get("input", {}))}})
            elif bt == "tool_result":
                rc = b.get("content", "")
                if isinstance(rc, list):
                    rc = "".join(x.get("text", "") for x in rc
                                 if isinstance(x, dict) and x.get("type") == "text")
                tool_msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id"),
                                  "content": rc if isinstance(rc, str) else json.dumps(rc)})
        # OpenAI carries tool results as standalone role:"tool" messages, not nested.
        if tool_msgs:
            messages.extend(tool_msgs)
            if any(text_parts):
                messages.append({"role": role, "content": "".join(text_parts)})
        else:
            msg = {"role": role, "content": "".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)

    payload: dict = {"model": CASCADE_MODEL, "messages": messages}
    if body.get("stream"):
        payload["stream"] = True
    for field in ("max_tokens", "temperature", "top_p"):
        if body.get(field) is not None:
            payload[field] = body[field]
    if body.get("stop_sequences"):
        payload["stop"] = body["stop_sequences"]
    if body.get("tools"):
        payload["tools"] = [{"type": "function", "function": {
            "name": t.get("name", ""), "description": t.get("description", ""),
            "parameters": t.get("input_schema", {})}}
            for t in body["tools"] if isinstance(t, dict) and t.get("name")]
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        ttype = tc.get("type")
        if ttype == "auto":
            payload["tool_choice"] = "auto"
        elif ttype == "any":
            payload["tool_choice"] = "required"
        elif ttype == "tool" and tc.get("name"):
            payload["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    return payload


def _openai_response_to_anthropic(data: dict) -> dict:
    """Convert an OpenAI chat-completion response to Anthropic Messages format,
    including assistant tool calls (-> tool_use content blocks)."""
    choice  = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish  = choice.get("finish_reason") or "stop"
    usage   = data.get("usage") or {}

    blocks = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        blocks.append({"type": "tool_use", "id": tc.get("id"),
                       "name": fn.get("name"), "input": args})
    if not blocks:
        blocks = [{"type": "text", "text": ""}]

    return {
        "id":            data.get("id", "msg_unknown"),
        "type":          "message",
        "role":          "assistant",
        "model":         data.get("model", CASCADE_MODEL),
        "content":       blocks,
        "stop_reason":   "tool_use" if tool_calls else _OPENAI_TO_ANTHROPIC_STOP.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens":  usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _replace_surrogates(obj):
    """Recursively replace lone surrogate codepoints (U+D800-U+DFFF)
    with U+FFFD (REPLACEMENT CHARACTER) so json.dumps() doesn't blow up."""
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="replace").decode("utf-8")
    elif isinstance(obj, dict):
        return {k: _replace_surrogates(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_replace_surrogates(v) for v in obj]
    return obj


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(_replace_surrogates(data))}\n\n"


def _openai_stream_to_anthropic(gen):
    """Translate an OpenAI-format SSE stream (bytes, as yielded by the routing
    pipeline) into the Anthropic Messages SSE event sequence the Anthropic SDK
    expects: message_start → (content_block_start → content_block_delta* →
    content_block_stop)* → message_delta → message_stop.

    Handles both text deltas (text_delta) and streamed tool calls
    (tool_use blocks with input_json_delta). Anthropic allows only one content
    block open at a time, so we close the current block before opening the next
    and give each OpenAI tool-call index its own Anthropic block."""
    msg_id   = f"msg_{int(time.time())}"
    model    = CASCADE_MODEL
    finish   = "stop"
    started  = False           # message_start emitted?
    saw_tool = False
    next_index  = 0            # next Anthropic content-block index to allocate
    open_kind   = None         # None | "text" | "tool"
    open_index  = None         # Anthropic index of the currently open block
    tool_blocks = {}           # OpenAI tool-call index -> Anthropic block index

    def message_start():
        return _sse("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})

    buf = ""
    for chunk in gen:
        if isinstance(chunk, (bytes, bytearray)):
            chunk = chunk.decode("utf-8", errors="replace")
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = _replace_surrogates(json.loads(data))
            except Exception:
                continue
            model  = obj.get("model") or model
            choice = (obj.get("choices") or [{}])[0]
            delta  = choice.get("delta") or {}
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]

            if not started:
                started = True
                yield message_start()

            # ---- text delta ----
            piece = delta.get("content")
            if piece:
                if open_kind != "text":
                    if open_kind is not None:
                        yield _sse("content_block_stop", {"type": "content_block_stop", "index": open_index})
                    open_index, open_kind = next_index, "text"
                    next_index += 1
                    yield _sse("content_block_start", {"type": "content_block_start",
                        "index": open_index, "content_block": {"type": "text", "text": ""}})
                yield _sse("content_block_delta", {"type": "content_block_delta",
                    "index": open_index, "delta": {"type": "text_delta", "text": piece}})

            # ---- tool-call deltas ----
            for tc in (delta.get("tool_calls") or []):
                saw_tool = True
                oai_idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                if oai_idx not in tool_blocks:            # first chunk for this tool call
                    if open_kind is not None:
                        yield _sse("content_block_stop", {"type": "content_block_stop", "index": open_index})
                    open_index, open_kind = next_index, "tool"
                    next_index += 1
                    tool_blocks[oai_idx] = open_index
                    yield _sse("content_block_start", {"type": "content_block_start",
                        "index": open_index, "content_block": {
                            "type": "tool_use", "id": tc.get("id") or f"toolu_{msg_id}_{oai_idx}",
                            "name": fn.get("name") or "", "input": {}}})
                if fn.get("arguments"):
                    yield _sse("content_block_delta", {"type": "content_block_delta",
                        "index": tool_blocks[oai_idx],
                        "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}})

    if not started:
        yield message_start()
    if open_kind is None:        # no content at all — emit an empty text block
        open_index = 0
        yield _sse("content_block_start", {"type": "content_block_start",
            "index": 0, "content_block": {"type": "text", "text": ""}})
    yield _sse("content_block_stop", {"type": "content_block_stop", "index": open_index})
    stop_reason = "tool_use" if saw_tool else _OPENAI_TO_ANTHROPIC_STOP.get(finish, "end_turn")
    yield _sse("message_delta", {"type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 0}})
    yield _sse("message_stop", {"type": "message_stop"})


def _anthropic_error(message: str) -> dict:
    """Anthropic-format error envelope."""
    return {"type": "error", "error": {"type": "api_error", "message": message}}

# ── Complexity-aware provider ordering ────────────────────────────────────────

# Accurate token counting via tiktoken when available. The encoder is loaded
# lazily on first use (not at import) so startup never blocks on tiktoken's
# one-time vocab download, and any failure (no tiktoken, offline, etc.) falls
# back to the character heuristic — cascade always works regardless.
_ENCODER = "uninitialized"  # sentinel; resolves to an encoder or None on first use


def _get_encoder():
    global _ENCODER
    if _ENCODER == "uninitialized":
        try:
            import tiktoken
            _ENCODER = tiktoken.get_encoding("o200k_base")
        except Exception as e:
            log.warning(f"tiktoken unavailable ({e}); using char/4 token estimate")
            _ENCODER = None
    return _ENCODER


def _message_text(m: dict) -> str:
    """Extract plain text from a message whose content is either a string or a
    list of multimodal parts (only text parts contribute to the token count)."""
    content = m.get("content", "")
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content)


def _estimated_tokens(messages: list) -> int:
    """Token count for a message list. Uses tiktoken for an accurate count when
    available, otherwise a characters/4 heuristic. Adds a small per-message
    framing overhead (~4 tokens) plus 3 priming tokens, matching how chat
    models actually bill structured messages."""
    enc = _get_encoder()
    if enc is not None:
        total = 3
        for m in messages:
            total += 4 + len(enc.encode(_message_text(m)))
        return total
    return sum(len(_message_text(m)) for m in messages) // 4


def _supports_tools(provider: dict) -> bool:
    """Whether this provider's model handles function calling, from the startup
    probe. Unknown (e.g. state from before this feature, or never probed) is
    treated optimistically as capable so we never hard-fail on missing data."""
    val = _provider_state.get(provider["name"], {}).get("supports_tools")
    return True if val is None else bool(val)


def _ordered_providers(payload: dict) -> list[dict]:
    """
    Smart complexity-aware ordering: use cheapest capable model for simple
    tasks, best model for complex ones. With FAST_ROUTE_THRESHOLD set,
    short requests break ties in favour of low-latency providers.

    If prompt-based routing has already pinned a specific model, restrict
    to providers that can actually serve that model; otherwise the cascade
    would still try every provider and silently override the choice.
    """
    messages          = payload.get("messages", [])
    complexity        = classify_complexity(messages)
    requested_model   = payload.get("model", "")
    providers         = PROVIDERS

    if requested_model and requested_model not in ("", CASCADE_MODEL, "auto", "any"):
        matching = [p for p in PROVIDERS if p.get("model") == requested_model]
        if matching:
            providers = matching
            log.info("→ model-pin %s -> providers=%s", requested_model, [p["name"] for p in providers])

    ordered = _get_smart_ordered(providers, complexity, _estimated_tokens(messages))
    log.info(f"→ complexity={complexity} ({_COMPLEXITY_LABELS[complexity]}) "
             f"order={[p['name'] for p in ordered]}")
    return ordered

# ── Request forwarding ─────────────────────────────────────────────────────────

def forward(provider: dict, key: str, payload: dict, streaming: bool) -> requests.Response | None:
    # Anthropic uses a different wire format — translate and send directly.
    if provider.get("protocol") == "anthropic":
        model = payload.get("model", "")
        if not model or model in ("", CASCADE_MODEL, "auto"):
            model = provider["model"]
        cleaned = []
        for msg in payload.get("messages", []):
            m = dict(msg)
            _strip_message(m)
            cleaned.append(m)
        body = _to_anthropic_body({**payload, "messages": cleaned}, model)
        hdrs = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        try:
            return _HTTP.post("https://api.anthropic.com/v1/messages",
                              headers=hdrs, json=body, stream=streaming, timeout=(10, 120))
        except requests.exceptions.RequestException as e:
            log.error(f"  Network error → anthropic: {e}")
            return None

    # Sanitize header values — HTTP headers must be latin-1 encodable.
    # Some corrupted keys (e.g. placeholder redaction text) contain
    # non-latin-1 characters (U+2026 …). Replace silently rather than
    # crashing with UnicodeEncodeError.
    def _sanitize_header(v: str) -> str:
        try:
            v.encode("latin-1")
            return v
        except UnicodeEncodeError:
            safe = v.encode("latin-1", errors="replace").decode("latin-1")
            log.warning("  sanitized non-latin-1 chars in header value")
            return safe

    headers = {
        "Authorization": f"Bearer {_sanitize_header(key)}",
        "Content-Type":  "application/json",
        **provider.get("headers", {}),
    }

    body = dict(payload)

    # Remap model name to the provider's real model. The model the client
    # sends is a cascade-level abstraction — always substitute to the
    # provider's actual model before forwarding.
    body["model"] = provider["model"]

    # Strip thinking fields from conversation history before forwarding
    if "messages" in body:
        cleaned = []
        for msg in body["messages"]:
            m = dict(msg)
            _strip_message(m)
            cleaned.append(m)
        body["messages"] = cleaned

    # Strip top-level thinking fields (Gemini sometimes adds these)
    body.pop("think", None)
    body.pop("thinking", None)

    # Reasoning models spend output tokens on hidden chain-of-thought, so a small
    # client max_tokens can be entirely consumed by thinking — leaving empty
    # content. Give reasoning providers extra headroom on top of what the client
    # asked for, so the actual answer still fits. (The model stops when done, so
    # short answers stay short.) Tune/disable with REASONING_TOKEN_RESERVE.
    if _provider_state.get(provider["name"], {}).get("reasoning"):
        reserve = _int_env("REASONING_TOKEN_RESERVE", 4096)
        if reserve > 0:
            for field in ("max_tokens", "max_completion_tokens"):
                if isinstance(body.get(field), int):
                    body[field] += reserve

    # Clamp the requested output length to this provider's hard ceiling. Some
    # providers (e.g. Cohere caps output at 8192) reject the ENTIRE request with
    # a 400 when max_tokens exceeds their limit — so a client default like
    # max_tokens=65536 would fail every call. Capping it lets the request through;
    # the model still produces up to its real maximum.
    out_cap = provider.get("max_output_tokens", 0)
    was_clamped = False
    if out_cap:
        for field in ("max_tokens", "max_completion_tokens"):
            if isinstance(body.get(field), int) and body[field] > out_cap:
                log.info(f"  clamping {field} {body[field]}→{out_cap} for {provider['name']}")
                body[field] = out_cap
                was_clamped = True

    url = provider["base_url"].rstrip("/") + "/chat/completions"
    try:
        return (_HTTP.post(url, headers=headers, json=body, stream=streaming, timeout=(10, 120)), was_clamped)
    except requests.exceptions.RequestException as e:
        log.error(f"  Network error → {provider['name']}: {e}")
        return (None, was_clamped)


def _embed_ordered() -> list[dict]:
    """Embedding-capable providers in a STABLE priority order — deliberately NOT
    round-robined like chat. Different providers return different vector
    dimensions (e.g. gemini 3072, cohere 1536, mistral 1024), and vectors of
    different dimensions can't be compared in one store. So we keep hitting the
    same provider and only fail over (accepting a dimension change) when it's
    actually down. Open breakers and unhealthy providers sink to the back; the
    sort is stable, so healthy providers keep their config order as the priority.

    For STRICT single-dimension guarantees, disable the others' embed models
    (e.g. MISTRAL_EMBED_MODEL= and COHERE_EMBED_MODEL= empty in .env)."""
    embed_providers = [p for p in PROVIDERS if p.get("embed_model")]
    return sorted(embed_providers, key=lambda p: (1 if stats.breaker_open(p["name"]) else 0,
                                                  stats.health_bucket(p["name"])))


def forward_embeddings(provider: dict, key: str, payload: dict) -> requests.Response | None:
    """POST an OpenAI-format embeddings request to a provider, substituting the
    provider's configured embed model. No streaming, no format translation."""
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        **provider.get("headers", {}),
    }
    body = dict(payload)
    body["model"] = provider["embed_model"]   # always the provider's real embed model
    url = provider["base_url"].rstrip("/") + "/embeddings"
    try:
        return _HTTP.post(url, headers=headers, json=body, timeout=(10, 120))
    except requests.exceptions.RequestException as e:
        log.error(f"  Network error → {provider['name']} embeddings: {e}")
        return None

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
# Cap request bodies so a buggy client can't exhaust memory (Flask returns 413)
app.config["MAX_CONTENT_LENGTH"] = _int_env("MAX_REQUEST_BYTES", 10 * 1024 * 1024)
START_TIME = time.time()   # for uptime in /metrics


def _auth_check():
    header = request.headers.get("Authorization", "").strip()
    token  = header[7:].strip() if header[:7].lower() == "bearer " else header
    if not token:
        # The Anthropic SDK sends the key via x-api-key, not Authorization.
        token = request.headers.get("x-api-key", "").strip()
    # Accept keys from both environment and Bitwarden so CASCADE_API_KEY
    # doesn't need to live in any .env file — Bitwarden is the SSOT.
    valid_keys = set(CASCADE_API_KEY)
    if _BW_KEYS and _BW_KEYS.get("CASCADE_API_KEY"):
        valid_keys.add(_BW_KEYS["CASCADE_API_KEY"])
    # compare_digest keeps the comparison constant-time per key
    if not any(hmac.compare_digest(token, k) for k in valid_keys):
        return jsonify({"error": "unauthorized"}), 401


@app.route("/health")
def health():
    """Unauthenticated health check for uptime monitoring."""
    return jsonify({"status": "ok", "providers": [p["name"] for p in PROVIDERS]})


@app.route("/v1/models")
def models():
    err = _auth_check()
    if err:
        return err
    return jsonify({"object": "list", "data": [
        {"id": CASCADE_MODEL, "object": "model", "owned_by": "cascade"}
    ]})


def _route_completion(payload: dict, streaming: bool):
    """Core routing + failover pipeline, shared by /v1/chat/completions and the
    Anthropic-compatible /v1/messages. Takes an OpenAI-format payload and returns
    one of:
        ("json",   data_dict)            non-streaming success (OpenAI format)
        ("stream", generator, provider)  streaming success; generator yields
                                         OpenAI-format SSE regardless of upstream
        ("error",  error_dict, status)   every provider exhausted
    """
    messages = payload.get("messages", [])
    trace_id = _uuid.uuid4().hex[:12]

    # Cache check (non-streaming only)
    if not streaming:
        cached = cache.get(payload)
        if cached is not None:
            log.info("[%s] ↩ cache hit", trace_id)
            return ("json", cached, trace_id)

    est_tokens = _estimated_tokens(messages)

    # ── Adaptive max_tokens ──────────────────────────────────────────────────
    # Don't waste output tokens on trivial queries.
    _client_mt = payload.get("max_tokens") or payload.get("max_completion_tokens") or 0
    _adaptive_mt = None
    if not payload.get("tools"):  # never cap tool calls
        if _client_mt <= 0 or _client_mt > 16384:
            content = " ".join(
                m["content"] if isinstance(m.get("content"), str)
                else " ".join(p.get("text", "") for p in m["content"] if isinstance(p, dict))
                for m in messages if m.get("content")
            )
            clen = len(content)
            if clen < 50:
                _adaptive_mt = 256
            elif clen < 200:
                _adaptive_mt = 512
            elif clen < 1000:
                _adaptive_mt = 2048
            else:
                _adaptive_mt = 4096
        elif _client_mt > 8192 and est_tokens < 2000:
            _adaptive_mt = 4096

    if _adaptive_mt and (_client_mt <= 0 or _adaptive_mt < _client_mt):
        log.info("[%s] → adaptive max_tokens: %d (client had %d, ~%d input tok)",
                 trace_id, _adaptive_mt, _client_mt, est_tokens)
        payload = dict(payload)
        payload["max_tokens"] = _adaptive_mt

    # Prompt-based routing: if the user supplied a matching keyword rule,
    # pin the model for this request BEFORE provider selection so the
    # cascade uses only providers that serve that model.
    _prompt_model = _pick_model_by_prompt(messages)
    if _prompt_model:
        log.info("[%s] → prompt-route matched model=%s", trace_id, _prompt_model)
        payload = dict(payload)
        payload["model"] = _prompt_model

    ordered    = _ordered_providers(payload)

    # Filter providers whose startup probe returned unavailable (e.g. auth
    # failures, model-id mismatches). This avoids wasting a round-trip on
    # providers that are known to fail. SAFETY: if filtering would leave zero
    # providers, fall through to all of them (half-open probe).
    avail = [p for p in ordered
             if _provider_state.get(p["name"], {}).get("available", True)]
    if len(avail) < len(ordered):
        if avail:
            log.info("[%s] → filtered %d unavailable provider(s): %s",
                     trace_id, len(ordered) - len(avail),
                     [p["name"] for p in ordered if p not in avail])
            ordered = avail
        else:
            log.info("[%s] → all providers unavailable — probing all (half-open)", trace_id)

    log.info("[%s] → ordered=%s", trace_id, [p["name"] for p in ordered])

    # Tool-aware routing: when the request carries tools, prefer providers whose
    # model actually supports function calling — otherwise a provider that
    # silently ignores tools would return plain text instead of the tool call.
    # SAFETY — only enforce this when at least one tool-capable provider is
    # available; if none are, fall through to all of them rather than hard-fail.
    needs_tools  = bool(payload.get("tools"))
    enforce_tool = needs_tools and any(_supports_tools(p) for p in ordered)

    # Circuit breaker: skip providers whose breaker is open. SAFETY — if EVERY
    # candidate is open, treat them all as half-open probes (skip none) so we
    # always make forward progress instead of hard-failing while options remain.
    any_closed = any(not stats.breaker_open(p["name"]) for p in ordered)

    for provider in ordered:
        name     = provider["name"]

        # Breaker open → skip (unless all are open, then probe everything).
        if any_closed and stats.breaker_open(name):
            log.info("[%s] ⨂ skipping %s (circuit open)", trace_id, name)
            continue

        # Tool request → skip providers whose model can't do function calling.
        if enforce_tool and not _supports_tools(provider):
            log.info("[%s] ⚒ skipping %s (no tool support)", trace_id, name)
            continue

        # Skip providers whose payload ceiling this request would exceed
        # (e.g. Groq's free TPM) — avoids a guaranteed 413 round-trip.
        cap = provider.get("skip_if_tokens_over", 0)
        if cap and est_tokens > cap:
            log.info("[%s] ⤳ skipping %s (~%d tok > %d cap)", trace_id, name, est_tokens, cap)
            continue

        # Bulkhead: limit concurrent in-flight requests per provider.
        if not bulkhead.try_acquire(name):
            log.info("[%s] ⏸ bulkhead full for %s", trace_id, name)
            continue

        attempts = len(pool.pools.get(name, [])) or 1

        try:
            for _ in range(attempts):
                key = pool.get_key(name)
                if not key:
                    log.warning("[%s] All %s keys cooling — skipping provider", trace_id, name)
                    break

                log.info("[%s] → Trying %s ...%s", trace_id, name, key[-6:])
                t0   = time.time()
                resp, was_clamped = forward(provider, key, payload, streaming)
                elapsed = time.time() - t0

                if resp is None:
                    stats.record_error(name)
                    stats.record_health(name, False)   # network/timeout = provider health failure
                    pool.mark_rate_limited(name, key, retry_after=30)
                    continue

                if resp.status_code == 429:
                    stats.record_error(name)
                    # 429 is NOT a health failure — key cooldown already handles it.
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    pool.mark_rate_limited(name, key, retry_after=retry_after)
                    log.warning("[%s]   %s 429 — cooldown %ds, trying next key", trace_id, name, retry_after)
                    continue

                if resp.status_code in (400, 401, 403):
                    stats.record_error(name)
                    # request/auth-specific — NOT a provider health failure.
                    log.error("[%s]   %s %d — skipping provider: %s",
                              trace_id, name, resp.status_code, resp.text[:200])
                    break

                if resp.status_code == 413:
                    stats.record_error(name)
                    # payload-specific — NOT a provider health failure.
                    log.warning("[%s]   %s 413 — payload too large, cascading", trace_id, name)
                    break

                if resp.status_code >= 500:
                    stats.record_error(name)
                    stats.record_health(name, False)   # 5xx = provider health failure
                    pool.mark_rate_limited(name, key, retry_after=15)
                    continue

                if not (200 <= resp.status_code < 300):
                    stats.record_error(name)
                    stats.record_health(name, False)   # unexpected non-2xx = health failure
                    log.warning("[%s]   %s unexpected %d — skipping provider", trace_id, name, resp.status_code)
                    break

                # Success
                stats.record_success(name, elapsed)
                stats.record_health(name, True)        # 2xx = healthy (half-open recovery)
                stats.record_trace(name, trace_id)
                log.info("[%s]   ✓ %s %d (%.0fms)", trace_id, name, resp.status_code, elapsed * 1000)

                # Output was clamped (e.g. Cohere 8K cap) — don't return truncated
                # result. Cascade to next provider which may handle full length.
                if was_clamped:
                    log.info("[%s]   ↻ clamped output — cascading to next provider for full response", trace_id)
                    break

                is_anthropic = provider.get("protocol") == "anthropic"
                if streaming:
                    gen = (_anthropic_streaming_generator(resp) if is_anthropic
                           else _streaming_generator(resp))
                    return ("stream", gen, name, trace_id)
                else:
                    data = (_from_anthropic_response(_replace_surrogates(resp.json())) if is_anthropic
                            else _replace_surrogates(resp.json()))
                    if not is_anthropic:
                        _strip_response(data)
                    # --- Cost tracking ---
                    usage = data.get("usage", {})
                    prompt_tok = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
                    completion_tok = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
                    cost = _estimate_cost(prompt_tok, completion_tok, provider.get("model", ""))
                    if cost > 0:
                        log.info("[%s]   $ cost=%.6f (%d+%d tok)", trace_id, cost, prompt_tok, completion_tok)
                        stats.record_cost(name, cost, prompt_tok, completion_tok)
                    cache.set(payload, data)
                    return ("json", data, trace_id)

            log.info("[%s] → %s done — trying next provider", trace_id, name)
        finally:
            bulkhead.release(name)

    return ("error", {"error": {"message": "All providers exhausted", "type": "router_error"}}, 503)


@app.route("/v1/chat/completions", methods=["POST"])
def chat():
    err = _auth_check()
    if err:
        return err

    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": {"message": "request body must be a JSON object",
                                  "type": "invalid_request_error"}}), 400

    result = _route_completion(payload, payload.get("stream", False))
    if result[0] == "json":
        _, data, trace_id = result
        return jsonify(data), 200, {"X-Trace-Id": trace_id}
    if result[0] == "stream":
        _, gen, name, trace_id = result
        return Response(stream_with_context(gen), content_type="text/event-stream",
                        headers={"X-Provider": name, "X-Trace-Id": trace_id})
    return jsonify(result[1]), result[2]


@app.route("/v1/messages", methods=["POST"])
def anthropic_messages():
    """Anthropic Messages API endpoint — lets the Anthropic SDK use cascade
    plug-and-play. The request is translated to OpenAI format, routed through the
    same multi-provider pipeline as /v1/chat/completions, and translated back."""
    err = _auth_check()
    if err:
        return err

    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or "messages" not in body:
        return jsonify(_anthropic_error("request body must be a JSON object with a 'messages' field")), 400

    streaming = bool(body.get("stream", False))
    payload   = _anthropic_request_to_openai(body)
    result    = _route_completion(payload, streaming)

    if result[0] == "json":
        _, data, trace_id = result
        return jsonify(_openai_response_to_anthropic(data)), 200, {"X-Trace-Id": trace_id}
    if result[0] == "stream":
        _, gen, name, trace_id = result
        return Response(stream_with_context(_openai_stream_to_anthropic(gen)),
                        content_type="text/event-stream",
                        headers={"X-Provider": name, "X-Trace-Id": trace_id})
    return jsonify(_anthropic_error(result[1].get("error", {}).get("message", "error"))), result[2]


@app.route("/v1/embeddings", methods=["POST"])
def embeddings():
    err = _auth_check()
    if err:
        return err

    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict) or "input" not in payload:
        return jsonify({"error": {"message": "request body must be a JSON object with an 'input' field",
                                  "type": "invalid_request_error"}}), 400

    ordered = _embed_ordered()
    if not ordered:
        return jsonify({"error": {"message": "no embedding-capable providers configured "
                                             "(set e.g. GEMINI_API_KEYS or MISTRAL_API_KEYS)",
                                  "type": "router_error"}}), 503

    # Embeddings are deterministic — identical input is a perfect cache hit.
    cached = cache.get(payload)
    if cached is not None:
        log.info("↩ cache hit (embeddings)")
        return jsonify(cached)

    any_closed = any(not stats.breaker_open(p["name"]) for p in ordered)

    for provider in ordered:
        name = provider["name"]
        if any_closed and stats.breaker_open(name):
            log.info(f"⨂ skipping {name} embeddings (circuit open)")
            continue

        attempts = len(pool.pools.get(name, [])) or 1
        for _ in range(attempts):
            key = pool.get_key(name)
            if not key:
                log.warning(f"All {name} keys cooling — skipping provider")
                break

            log.info(f"→ Trying {name} embeddings ({provider['embed_model']}) ...{key[-6:]}")
            t0   = time.time()
            resp = forward_embeddings(provider, key, payload)
            elapsed = time.time() - t0

            if resp is None:
                stats.record_error(name); stats.record_health(name, False)
                pool.mark_rate_limited(name, key, retry_after=30)
                continue
            if resp.status_code == 429:
                stats.record_error(name)
                pool.mark_rate_limited(name, key, retry_after=_parse_retry_after(resp.headers.get("Retry-After")))
                log.warning(f"  {name} 429 — cooldown, trying next key")
                continue
            if resp.status_code in (400, 401, 403, 404):
                stats.record_error(name)   # request/auth/model-specific, not a health failure
                log.error(f"  {name} embeddings {resp.status_code} — skipping provider: {resp.text[:200]}")
                break
            if resp.status_code >= 500:
                stats.record_error(name); stats.record_health(name, False)
                pool.mark_rate_limited(name, key, retry_after=15)
                continue
            if not (200 <= resp.status_code < 300):
                stats.record_error(name); stats.record_health(name, False)
                log.warning(f"  {name} embeddings unexpected {resp.status_code} — skipping provider")
                break

            stats.record_success(name, elapsed); stats.record_health(name, True)
            log.info(f"  ✓ {name} embeddings ({elapsed*1000:.0f}ms)")
            data = resp.json()
            cache.set(payload, data)
            return jsonify(data), 200

        log.warning(f"✗ {name} embeddings exhausted — cascading")

    return jsonify({"error": {"message": "All embedding providers exhausted", "type": "router_error"}}), 503


@app.route("/v1/status")
def status():
    """Show key cooldown state, latency/error stats, and cache metrics."""
    err = _auth_check()
    if err:
        return err

    now  = time.time()
    keys = {}
    with pool.lock:
        for name, entries in pool.pools.items():
            keys[name] = [
                {
                    "key_tail": e["key"][-6:],
                    "status":   "cooling" if e["cool_until"] > now else "ready",
                    "ready_in": max(0, round(e["cool_until"] - now)),
                }
                for e in entries
            ]

    provider_stats = {}
    for p in PROVIDERS:
        entry = {
            "keys":  keys.get(p["name"], []),
            "stats": stats.summary(p["name"]),
            "breaker": stats.breaker_status(p["name"]),
        }
        # Surface the internal routing signals (rating + probe latency + model)
        # so dashboards can show them. Added only when known, so un-probed
        # providers still fall back to the dashboard's "?"/"—" placeholders.
        st = _provider_state.get(p["name"], {})
        if st.get("rating") is not None:
            entry["rating"] = st["rating"]
        if st.get("latency_ms"):
            entry["latency_ms"] = st["latency_ms"]
        if st.get("model"):
            entry["model"] = st["model"]
        if "available" in st:
            entry["available"] = st["available"]
        if "supports_tools" in st:
            entry["supports_tools"] = st["supports_tools"]
        if "reasoning" in st:
            entry["reasoning"] = st["reasoning"]
        if p.get("skip_if_tokens_over"):
            entry["skip_if_tokens_over"] = p["skip_if_tokens_over"]
        if p.get("max_output_tokens"):
            entry["max_output_tokens"] = p["max_output_tokens"]
        provider_stats[p["name"]] = entry

    return jsonify({
        "providers": provider_stats,
        "cache": {
            "enabled":  CACHE_TTL > 0,
            "ttl_s":    CACHE_TTL,
            "size":     cache.size,
            "max_size": CACHE_MAX_SIZE,
            "hits":     cache.hits,
            "misses":   cache.misses,
            "hit_rate": cache.hit_rate,
        },
        "fast_routing": {
            "enabled":         FAST_ROUTE_TOKENS > 0,
            "threshold_tokens": FAST_ROUTE_TOKENS,
            "fast_providers":  sorted(_FAST_PROVIDERS),
        },
        "circuit_breaker": {
            "window":      BREAKER_WINDOW,
            "min_samples": BREAKER_MIN_SAMPLES,
            "error_rate":  BREAKER_ERROR_RATE,
            "cooldown_s":  BREAKER_COOLDOWN,
        },
        "bulkhead": {
            "enabled":       BULKHEAD_MAX > 0,
            "max_concurrent": BULKHEAD_MAX,
            "per_provider":  bulkhead.status(),
        },
        "trace": {
            "enabled":       TRACE_ENABLED,
            "header":        "X-Trace-Id",
        },
        "cost_tracking": {
            "enabled":       len(KNOWN_MODEL_COSTS) > 0,
            "models_priced": len(KNOWN_MODEL_COSTS),
        },
    })


@app.route("/metrics")
def metrics():
    """Prometheus text-format metrics for scraping (Grafana, etc.). Exposes only
    counts and timings — never request content — so it's unauthenticated like
    /health. Set METRICS_REQUIRE_AUTH=1 to require the proxy key instead."""
    if _int_env("METRICS_REQUIRE_AUTH", 0):
        err = _auth_check()
        if err:
            return err

    out: list[str] = []

    def emit(name, mtype, help_, samples):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {mtype}")
        for labels, val in samples:
            tag = ("{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}") if labels else ""
            out.append(f"{name}{tag} {val}")

    emit("cascade_uptime_seconds", "gauge", "Seconds since cascade started",
         [({}, round(time.time() - START_TIME))])
    emit("cascade_providers", "gauge", "Number of configured providers",
         [({}, len(PROVIDERS))])

    req, errs, lat, brk, cost_m = [], [], [], [], []
    for p in PROVIDERS:
        name = p["name"]
        s = stats.summary(name)
        req.append(({"provider": name}, s["total_requests"]))
        errs.append(({"provider": name}, s["errors"]))
        if s["avg_latency_ms"] is not None:
            lat.append(({"provider": name}, s["avg_latency_ms"]))
        brk.append(({"provider": name}, 1 if stats.breaker_open(name) else 0))
        cost_m.append(({"provider": name}, s["cost_total_usd"]))
    emit("cascade_requests_total", "counter", "Total requests routed per provider", req)
    emit("cascade_errors_total", "counter", "Total errored requests per provider", errs)
    emit("cascade_avg_latency_ms", "gauge", "Mean successful-request latency in ms per provider", lat)
    emit("cascade_circuit_breaker_open", "gauge", "1 if the provider's circuit breaker is open, else 0", brk)
    emit("cascade_cost_total_usd", "counter", "Total estimated USD cost per provider", cost_m)

    emit("cascade_cache_hits_total", "counter", "Response-cache hits", [({}, cache.hits)])
    emit("cascade_cache_misses_total", "counter", "Response-cache misses", [({}, cache.misses)])
    emit("cascade_cache_size", "gauge", "Entries currently in the response cache", [({}, cache.size)])

    # Bulkhead metrics
    bh = bulkhead.status()
    emit("cascade_bulkhead_max", "gauge", "Per-provider bulkhead limit", [({}, BULKHEAD_MAX)])
    for p_name, bd in bh.items():
        if bd["active"] > 0:
            emit("cascade_bulkhead_active", "gauge", "Active requests per provider",
                 [({"provider": p_name}, bd["active"])])

    return Response("\n".join(out) + "\n", content_type="text/plain; version=0.0.4")


if __name__ == "__main__":
    log.info(f"cascade starting on :{PORT}")
    log.info(f"Providers: {[p['name'] for p in PROVIDERS]}")
    _embed = {p["name"]: p["embed_model"] for p in PROVIDERS if p.get("embed_model")}
    log.info(f"Embeddings (/v1/embeddings): {_embed if _embed else 'no embed-capable providers'}")
    log.info(f"Cache: {'enabled' if CACHE_TTL > 0 else 'disabled'} (TTL={CACHE_TTL}s, max={CACHE_MAX_SIZE})")
    log.info(f"Fast routing: {'enabled' if FAST_ROUTE_TOKENS > 0 else 'disabled'} (threshold={FAST_ROUTE_TOKENS} tokens)")
    log.info(f"Bulkhead: {'enabled' if BULKHEAD_MAX > 0 else 'disabled'} (max={BULKHEAD_MAX} concurrent/provider)")
    log.info(f"Cost tracking: {len(KNOWN_MODEL_COSTS)} models priced")
    _skips = {p["name"]: p["skip_if_tokens_over"] for p in PROVIDERS if p.get("skip_if_tokens_over")}
    if _skips:
        log.info(f"Large-payload skip ceilings: {_skips}")
    try:
        from waitress import serve
        log.info("Serving with waitress (production WSGI)")
        serve(app, host="0.0.0.0", port=PORT, threads=int(os.environ.get("WORKER_THREADS", 16)))
    except ImportError:
        log.warning("waitress not installed — falling back to Flask dev server")
        app.run(host="0.0.0.0", port=PORT, threaded=True)
