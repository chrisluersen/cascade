#!/usr/bin/env python3
"""
test_cascade.py — exhaustive test suite for the Cascade Router

Tests every new feature: trace IDs, cost tracking, bulkheads, provider filtering,
adaptive max_tokens, cache, fast routing, model pinning, and error handling.

Usage:
    python test_cascade.py              # run all tests
    python test_cascade.py -v           # verbose output
    python test_cascade.py TestTrace    # specific test class

Requirements: cascade must be running on localhost:8319
"""

import functools
import json
import os
import sys
import time
import urllib.error
import urllib.request

# ── Test configuration ──────────────────────────────────────────────────────

BASE = os.environ.get("CASCADE_BASE", "http://127.0.0.1:8319")


def _resolve_auth_key() -> str:
    """Resolve the router key the way cascade-probe.py does: env -> Hermes .env.

    Preferring CASCADE_API_KEY is load-bearing, not cosmetic. This file used to
    read ONLY `CASCADE_AUTH_KEY` (default "sk-router-1"), while the probe, the
    watchdog and the host environment all use `CASCADE_API_KEY`. Every test
    therefore sent a stale default and the suite produced 23 failures that were
    all the same 401 -- invisible for as long as the @test wrapper swallowed
    AssertionError (see the decorator's comment below).
    """
    for var in ("CASCADE_API_KEY", "CASCADE_AUTH_KEY"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    env_file = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hermes", ".env"
    )
    try:
        with open(env_file, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("CASCADE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "sk-router-1"


AUTH_KEY = _resolve_auth_key()
BAD_KEY = "sk-invalid-test-key-12345"

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {AUTH_KEY}",
}
BAD_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {BAD_KEY}",
}

# ── Helpers ──────────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0
SKIP = 0


def _req(endpoint: str, data: dict | None = None, headers: dict | None = None) -> tuple[int, dict, dict]:
    """Make a request to cascade. Returns (status_code, body_dict, headers_dict)."""
    h = headers or HEADERS
    url = f"{BASE}{endpoint}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if data else "GET")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        resp_body = json.loads(resp.read().decode())
        resp_headers = dict(resp.headers)
        return (resp.status, resp_body, resp_headers)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:500]
        try:
            err_body = json.loads(err_body)
        except json.JSONDecodeError:
            pass
        return (e.code, err_body, dict(e.headers))


def _chat(model: str = "any", msg: str = "say hi", max_tokens: int = 30, **kw) -> tuple[int, dict, dict]:
    """Quick chat completion helper."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": msg.split("|")}],
        "max_tokens": max_tokens,
        **kw,
    }
    # Flatten multi-message
    if isinstance(payload["messages"][0]["content"], list):
        payload["messages"] = payload["messages"][0]["content"]
        if isinstance(payload["messages"][0], str):
            payload["messages"] = [{"role": "user", "content": m} for m in payload["messages"]]
    return _req("/v1/chat/completions", payload)


def _status() -> tuple[int, dict, dict]:
    return _req("/v1/status")


def _health() -> int:
    code, _, _ = _req("/health")
    return code


# ── Test framework ───────────────────────────────────────────────────────────

# pytest collects any module attribute named `test*`. That collides with two
# things in this file, and both halves of the collision had a real cost:
#
#   * `test` itself is the decorator FACTORY, not a test, so pytest tried to
#     call it and reported `ERROR test_cascade.py::test -- fixture 'name' not
#     found` on every run.
#   * every decorated function is the inner `wrapper`, which CATCHES
#     AssertionError and increments FAIL instead of raising. pytest therefore
#     inherited a suite that COULD NOT FAIL: injecting an unconditional
#     `assert False` into a test still produced "33 passed"
#     (proven by scratch/falsify-pytest-teeth.py, before/after).
#
# Fix: hide the factory from collection, and let a failure actually raise when
# the run is pytest's. The hand-rolled runner below still tallies every failure
# in one pass -- that is what the PASS/FAIL counters and their summary are for,
# and its behaviour is unchanged.
_UNDER_PYTEST = "pytest" in sys.modules


def test(name: str):
    """Decorator for test functions. Handles pass/fail counting."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            global PASS, FAIL
            try:
                fn(*args, **kwargs)
                PASS += 1
                if not _UNDER_PYTEST:
                    print(f"  ✓ {name}")
            except Exception as e:
                FAIL += 1
                if _UNDER_PYTEST:
                    raise  # a pytest run must be ABLE to fail
                print(f"  ✗ {name}: {e}")
        return wrapper
    return decorator


test.__test__ = False  # a decorator factory, not a test — do not collect it


# ── Health tests ─────────────────────────────────────────────────────────────

@test("Health endpoint returns 200")
def test_health():
    code = _health()
    assert code == 200, f"expected 200, got {code}"


# ── Basic routing tests ──────────────────────────────────────────────────────

@test("Chat completion returns valid response with choices")
def test_basic_chat():
    code, body, _ = _chat()
    assert code == 200, f"expected 200, got {code}"
    assert "choices" in body, f"missing 'choices' in response: {list(body.keys())}"
    assert len(body["choices"]) > 0, "empty choices"
    assert "message" in body["choices"][0], "missing message in choice"
    assert "content" in body["choices"][0]["message"], "missing content"
    assert len(body["choices"][0]["message"]["content"]) > 0, "empty content"


@test("Response includes model and usage fields")
def test_response_fields():
    code, body, _ = _chat()
    assert code == 200
    assert "model" in body, "missing model"
    assert "usage" in body, "missing usage"
    assert "prompt_tokens" in body["usage"]
    assert "completion_tokens" in body["usage"]


@test("Streaming response works (basic)")
def test_streaming():
    code, body, _ = _chat(stream=True, max_tokens=5)
    assert code == 200, f"streaming expected 200, got {code}"
    # With streaming, cascade returns None for body if using actual SSE
    # But the endpoint should at least not error
    # Skip if body indicates streaming route
    assert code == 200


@test("Multi-turn conversation works")
def test_multi_turn():
    messages = [
        {"role": "user", "content": "My name is TestBot"},
        {"role": "assistant", "content": "Hello TestBot!"},
        {"role": "user", "content": "What is my name?"},
    ]
    code, body, _ = _req("/v1/chat/completions", {
        "model": "any",
        "messages": messages,
        "max_tokens": 30,
    })
    assert code == 200, f"expected 200, got {code}"
    content = body["choices"][0]["message"]["content"].lower()
    assert "testbot" in content, f"expected 'testbot' in response: {content[:100]}"


# ── Trace ID tests ───────────────────────────────────────────────────────────

@test("Response includes X-Trace-Id header")
def test_trace_id_present():
    _, _, headers = _chat()
    assert "X-Trace-Id" in headers, f"missing X-Trace-Id header: {list(headers.keys())}"
    trace_id = headers["X-Trace-Id"]
    assert len(trace_id) > 0, "empty trace_id"
    assert len(trace_id) >= 12, f"trace_id too short: {trace_id}"


@test("Trace IDs are unique per request")
def test_trace_id_unique():
    ids = set()
    for _ in range(10):
        _, _, headers = _chat(msg="test trace", max_tokens=5)
        trace_id = headers.get("X-Trace-Id", "MISSING")
        assert trace_id != "MISSING", "missing X-Trace-Id"
        assert trace_id not in ids, f"duplicate trace_id: {trace_id}"
        ids.add(trace_id)
    assert len(ids) == 10, f"expected 10 unique IDs, got {len(ids)}"


@test("Trace ID is logged in status response (no header needed)")
def test_trace_id_status():
    code, body, _ = _status()
    assert code == 200
    assert body.get("trace", {}).get("enabled") == True, "trace.enabled != True in status"
    assert body.get("trace", {}).get("header") == "X-Trace-Id", \
        f"trace.header wrong: {body.get('trace', {}).get('header')}"


# ── Cost tracking tests ──────────────────────────────────────────────────────

@test("Cost tracking reports enabled in status")
def test_cost_tracking_status():
    code, body, _ = _status()
    assert code == 200
    ct = body.get("cost_tracking", {})
    assert ct.get("enabled") == True, "cost_tracking.enabled != True"
    assert ct.get("models_priced", 0) > 0, f"no models priced: {ct}"


@test("Provider stats include cost_total_usd after requests")
def test_cost_tracking_accumulates():
    code, body, _ = _status()
    assert code == 200
    # Check that at least one provider has non-zero cost
    providers_with_cost = []
    for name, p in body.get("providers", {}).items():
        stats = p.get("stats", {})
        cost = stats.get("cost_total_usd", 0)
        if cost and cost > 0:
            providers_with_cost.append((name, cost))
            assert isinstance(cost, (int, float)), f"cost_total_usd not numeric: {cost}"
    # At least nous_portal should have accumulated cost from prior requests
    # (server has been running and processing requests)
    assert len(providers_with_cost) > 0, \
        f"no providers with non-zero cost: {[(n, p.get('stats',{}).get('cost_total_usd')) for n,p in body.get('providers',{}).items()]}"


@test("Cost estimate function works correctly")
def test_cost_estimate():
    # Inline the known cost function to avoid importing cascade.py
    KNOWN_MODEL_COSTS = {
        "deepseek/deepseek-v4-flash":                          (0.098, 0.196),
        "deepseek/deepseek-v4-pro":                            (0.350, 0.700),
        "deepseek/deepseek-r1":                                (0.014, 0.028),
        "llama-3.3-70b-versatile":                             (0.000, 0.000),  # free
    }

    def _estimate_cost(prompt_tok, completion_tok, model):
        costs = KNOWN_MODEL_COSTS.get(model)
        if costs is None:
            for key in KNOWN_MODEL_COSTS:
                if model.startswith(key):
                    costs = KNOWN_MODEL_COSTS[key]
                    break
        if not costs:
            return 0.0
        ppk, cpk = costs
        return (prompt_tok / 1_000_000.0 * ppk) + (completion_tok / 1_000_000.0 * cpk)

    # DeepSeek V4 Flash: $0.098/$0.196 per 1M tok
    cost = _estimate_cost(1_000_000, 0, "deepseek/deepseek-v4-flash")
    assert abs(cost - 0.098) < 0.001, f"deepseek flash prompt cost: expected 0.098, got {cost}"
    cost = _estimate_cost(0, 1_000_000, "deepseek/deepseek-v4-flash")
    assert abs(cost - 0.196) < 0.001, f"deepseek flash completion cost: expected 0.196, got {cost}"
    # Free model
    cost = _estimate_cost(1000, 500, "llama-3.3-70b-versatile")
    assert cost == 0.0, f"free model should cost 0, got {cost}"
    # Unknown model
    cost = _estimate_cost(100, 100, "unknown/model-v99")
    assert cost == 0.0, f"unknown model should cost 0, got {cost}"
    # Test prefix matching
    cost = _estimate_cost(1_000_000, 0, "deepseek/deepseek-v4-flash-20260423")
    assert abs(cost - 0.098) < 0.001, f"prefix match: expected 0.098, got {cost}"


# ── Bulkhead tests ───────────────────────────────────────────────────────────

@test("Bulkhead is enabled in status")
def test_bulkhead_enabled():
    code, body, _ = _status()
    assert code == 200
    bh = body.get("bulkhead", {})
    assert bh.get("enabled") == True, "bulkhead not enabled"
    assert bh.get("max_concurrent", 0) > 0, "max_concurrent not > 0"


@test("Bulkhead tracks active requests per provider")
def test_bulkhead_active():
    code, body, _ = _status()
    assert code == 200
    bh = body.get("bulkhead", {})
    per_provider = bh.get("per_provider", {})
    assert len(per_provider) > 0, "empty per_provider in bulkhead"
    # All providers should have valid active/max counts
    for name, state in per_provider.items():
        assert "active" in state, f"{name} missing 'active'"
        assert "max" in state, f"{name} missing 'max'"
        assert state["active"] >= 0, f"{name} negative active: {state['active']}"
        assert state["max"] == bh["max_concurrent"], \
            f"{name} max {state['max']} != global {bh['max_concurrent']}"


# ── Provider filtering tests ─────────────────────────────────────────────────

@test("Providers show availability status")
def test_provider_availability():
    code, body, _ = _status()
    assert code == 200
    providers = body.get("providers", {})
    assert len(providers) > 0, "no providers in status"
    for name, p in providers.items():
        assert "available" in p, f"{name} missing 'available'"
        assert isinstance(p["available"], bool), f"{name} available not bool"


@test("Status shows at least one available provider")
def test_at_least_one_available():
    code, body, _ = _status()
    assert code == 200
    available = [n for n, p in body.get("providers", {}).items() if p.get("available")]
    assert len(available) > 0, f"no available providers: {list(body.get('providers', {}).keys())}"
    print(f"      Available: {', '.join(available)}")


# ── Model pinning tests ──────────────────────────────────────────────────────

@test("Model pinning routes to specific model when requested")
def test_model_pinning():
    # Request a known model — cascade should route to a provider that serves it
    code, body, _ = _chat(model="llama-3.3-70b-versatile", msg="hello", max_tokens=10)
    assert code == 200, f"model pinning failed: {code}"
    # groq serves llama-3.3-70b-versatile
    model_returned = body.get("model", "")
    assert "llama" in model_returned.lower() or "groq" in model_returned.lower(), \
        f"expected llama-compatible model, got: {model_returned}"


@test("DeepSeek V4 Flash routes through nous_portal")
def test_deepseek_routing():
    code, body, _ = _chat(model="deepseek/deepseek-v4-flash", msg="say 1+1", max_tokens=15)
    assert code == 200, f"deepseek routing failed: {code}"
    model_returned = body.get("model", "")
    # Could be deepseek-v4-flash or an alias
    assert "deepseek" in model_returned.lower(), \
        f"expected deepseek model, got: {model_returned}"


# ── Adaptive max_tokens tests ────────────────────────────────────────────────

@test("Request with max_tokens=0 gets adaptive allocation")
def test_adaptive_max_tokens():
    code, body, _ = _chat(max_tokens=0, msg="Write a three-paragraph essay about the history of databases.")
    assert code == 200, f"expected 200, got {code}"
    usage = body.get("usage", {})
    completion = usage.get("completion_tokens", 0)
    assert completion > 10, f"adaptive allocation: only {completion} completion tokens (expected robust response)"
    print(f"      completion_tokens={completion} with max_tokens=0")


# ── Cache tests ──────────────────────────────────────────────────────────────

@test("Cache reports hit rate and counts in status")
def test_cache_status():
    code, body, _ = _status()
    assert code == 200
    cache = body.get("cache", {})
    assert "enabled" in cache, "cache missing 'enabled'"
    assert "hits" in cache, "cache missing 'hits'"
    assert "misses" in cache, "cache missing 'misses'"
    assert "hit_rate" in cache, "cache missing 'hit_rate'"
    assert isinstance(cache["hit_rate"], (int, float)), "hit_rate not numeric"


@test("Repeated identical requests hit the cache")
def test_cache_hits():
    msg = "What is the capital of France? Answer in one word."
    # First request — miss
    code1, body1, _ = _chat(msg=msg, max_tokens=10)
    assert code1 == 200

    # Second request with identical payload
    code2, body2, _ = _chat(msg=msg, max_tokens=10)
    assert code2 == 200

    # Same content suggests cache hit
    c1 = body1["choices"][0]["message"]["content"]
    c2 = body2["choices"][0]["message"]["content"]
    assert c1.strip() == c2.strip(), \
        f"cache should return same response: \n  first={c1}\n  second={c2}"


# ── Fast routing tests ───────────────────────────────────────────────────────

@test("Fast routing configuration is visible in status")
def test_fast_routing_status():
    code, body, _ = _status()
    assert code == 200
    fr = body.get("fast_routing", {})
    assert fr.get("enabled") == True, "fast_routing not enabled"
    assert fr.get("threshold_tokens", 0) > 0, f"threshold_tokens not > 0: {fr}"
    assert len(fr.get("fast_providers", [])) > 0, "no fast_providers configured"


# ── Error handling tests ─────────────────────────────────────────────────────

@test("Invalid auth returns 401")
def test_invalid_auth():
    code, body, _ = _req("/v1/chat/completions", {
        "model": "any",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
    }, headers=BAD_HEADERS)
    assert code == 401, f"expected 401 for bad auth, got {code}"


@test("Missing model returns 400 or routes gracefully")
def test_missing_model():
    code, body, _ = _req("/v1/chat/completions", {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
    })
    # Should either error gracefully or default to "any"
    if code == 400:
        assert True
    else:
        assert "choices" in body, f"expected choices or error, got {list(body.keys())}"


# ── Status endpoint tests ────────────────────────────────────────────────────

@test("Status endpoint has all new sections")
def test_status_sections():
    code, body, _ = _status()
    assert code == 200
    required_sections = ["bulkhead", "trace", "cost_tracking", "cache",
                         "circuit_breaker", "fast_routing", "providers"]
    for section in required_sections:
        assert section in body, f"missing '{section}' in status: {list(body.keys())}"


@test("Provider stats have all new tracking fields")
def test_provider_stats_fields():
    code, body, _ = _status()
    assert code == 200
    for name, p in body.get("providers", {}).items():
        stats = p.get("stats", {})
        required_stats = ["cost_total_usd", "prompt_tokens", "completion_tokens",
                          "total_requests", "errors", "error_rate", "avg_latency_ms"]
        for field in required_stats:
            assert field in stats, f"{name} stats missing '{field}': {list(stats.keys())}"


@test("Status endpoint response time is fast (< 500ms)")
def test_status_latency():
    start = time.time()
    code, _, _ = _status()
    elapsed = (time.time() - start) * 1000
    assert code == 200
    assert elapsed < 500, f"status took {elapsed:.0f}ms (expected < 500ms)"
    print(f"      response time: {elapsed:.0f}ms")


# ── Metrics endpoint test ────────────────────────────────────────────────────

@test("Metrics endpoint returns prometheus-format data")
def test_metrics():
    req = urllib.request.Request(f"{BASE}/metrics", headers=HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode()
        code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
        body = e.read().decode()[:500]
    assert code == 200, f"metrics expected 200, got {code}"
    assert "cascade_cost_total_usd" in body or \
           "cascade_bulkhead_max" in body or \
           "cascade_" in body, \
        f"expected cascade metrics in response: {body[:200]}"


# ── Run all tests ────────────────────────────────────────────────────────────

def main():
    global PASS, FAIL, SKIP

    print("═══ Cascade Router Test Suite ═══")
    print(f"Target: {BASE}")
    print()

    # Health check first
    try:
        code = _health()
        assert code == 200
        print(f"✓ Server reachable ({code})\n")
    except Exception as e:
        print(f"✗ Server unreachable: {e}")
        print("  Start cascade with: cd ~/.local/share/cascade && PROXY_API_KEYS=sk-router-1 venv/Scripts/python cascade.py")
        sys.exit(1)

    # Collect all test functions
    import __main__
    test_fns = []
    for name in dir(__main__):
        if name.startswith("test_") and callable(getattr(__main__, name)):
            fn = getattr(__main__, name)
            if hasattr(fn, "__wrapped__"):
                test_fns.append(fn)
            elif not hasattr(fn, "__test_function__"):
                # Locate the wrapper
                for obj_name in dir(__main__):
                    obj = getattr(__main__, obj_name)
                    if hasattr(obj, "__wrapped__") and obj.__wrapped__ is fn:
                        test_fns.append(obj)
                        break

    # If that fails, just iterate module directly
    if not test_fns:
        for name in dir(__main__):
            fn = getattr(__main__, name)
            if name.startswith("test_") and hasattr(fn, "__test_function__"):
                test_fns.append(fn)

    # Fall back to the imported test functions
    if not test_fns:
        # Redefine — use the module's function directly
        for name in sorted(dir()):
            if name.startswith("test_") and callable(globals().get(name)):
                fn = globals()[name]
                if callable(fn):
                    test_fns.append(fn)

    # Execute tests
    test_list = sorted([n for n in dir() if n.startswith("test_") and callable(globals()[n])])
    for name in test_list:
        fn = globals()[name]

    def run_all():
        for name in test_list:
            fn = globals()[name]
            try:
                fn()
            except Exception as e:
                global FAIL
                print(f"  ✗ {name}: {e}")
                FAIL += 1

    run_all()

    # Summary
    total = PASS + FAIL + SKIP
    print()
    print(f"═══ Results: {PASS} passed, {FAIL} failed, {SKIP} skipped ({total} total) ═══")
    return 0 if FAIL == 0 else 1


# ── Free-Only Mode (unit tests — no server required) ─────────────────────────
# These import cascade.py directly and exercise the router's own code, so they
# verify the FEATURE implementation rather than whatever the live server happens
# to be running. They never restart the service.
#
# The three required cases:
#   4a  free-only ON excludes every paid provider from the candidate list
#   4b  a provider that just failed is not retried in the same request
#   4c  free-only OFF restores the previous behaviour unchanged
# plus the two gate-level leak paths (unpriced model; /v1/embeddings).

import importlib.util as _ilu

_CASCADE_MOD = None


def _load_cascade():
    """Import cascade.py from this file's directory, once, in-process."""
    global _CASCADE_MOD
    if _CASCADE_MOD is not None:
        return _CASCADE_MOD
    here = os.path.dirname(os.path.abspath(__file__))
    spec = _ilu.spec_from_file_location("cascade_under_test", os.path.join(here, "cascade.py"))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)          # __main__ guard means no server starts
    _CASCADE_MOD = mod
    return mod


# Synthetic provider set: two genuinely free, two paid (one of them with a
# free-PRICED model so only the dual gate catches it), one unpriced.
_FO_PROVIDERS = [
    {"name": "free_a",   "model": "gemini-2.5-flash-lite",     "cost": 0},
    {"name": "free_b",   "model": "llama-3.3-70b-versatile",   "cost": 0},
    {"name": "paid_a",   "model": "anthropic/claude-sonnet-5", "cost": 2},
    {"name": "paid_b",   "model": "gpt-4o-mini",               "cost": 1},
    {"name": "unpriced", "model": "totally-unpriced-model-xyz", "cost": 0},
]


@test("Free-only ON excludes every paid provider from the candidate list (4a)")
def test_free_only_on_excludes_paid():
    c = _load_cascade()
    saved = c.PROVIDERS
    try:
        c.PROVIDERS = _FO_PROVIDERS
        payload = {"model": "cascade", "messages": [{"role": "user", "content": "hi"}]}

        free = [p["name"] for p in c._ordered_providers(payload, free_only=True)]
        assert "paid_a" not in free, f"paid_a leaked into free-only candidates: {free}"
        assert "paid_b" not in free, f"paid_b (free-priced model, paid tier) leaked: {free}"
        assert "unpriced" not in free, f"unpriced provider leaked (should deny): {free}"
        assert set(free) == {"free_a", "free_b"}, f"unexpected free-only candidate set: {free}"

        # Free-only OFF must include the paid providers — proves the flag, not
        # something else, is what removed them.
        allp = [p["name"] for p in c._ordered_providers(payload, free_only=False)]
        assert {"paid_a", "paid_b"} <= set(allp), \
            f"paid providers missing when free-only OFF: {allp}"

        # Leak path (b): an explicit paid model pin must not be honoured. With
        # free providers available it falls through to the free cascade; the
        # pinned paid provider must be gone.
        pin_payload = {"model": "anthropic/claude-sonnet-5",
                       "messages": [{"role": "user", "content": "hi"}]}
        pinned_names = [p["name"] for p in c._ordered_providers(dict(pin_payload), free_only=True)]
        assert "paid_a" not in pinned_names, \
            f"explicit paid model pin leaked past free-only: {pinned_names}"
        assert all(c._provider_is_free(p) for p in
                   c._ordered_providers(dict(pin_payload), free_only=True)), \
            f"non-free provider in free-only candidates: {pinned_names}"

        # ...and the pin still restricts exactly as before with free-only OFF.
        pin_off = [p["name"] for p in c._ordered_providers(dict(pin_payload), free_only=False)]
        assert pin_off == ["paid_a"], f"model-pin behaviour changed with free-only OFF: {pin_off}"
    finally:
        c.PROVIDERS = saved


@test("Free-only ON fails closed with 503 when no free provider can serve")
def test_free_only_fails_closed():
    c = _load_cascade()
    saved = c.PROVIDERS
    try:
        # Every provider is non-free (paid tier, or unpriced which is now denied).
        c.PROVIDERS = [p for p in _FO_PROVIDERS if not c._provider_is_free(p)]
        assert c.PROVIDERS, "test fixture has no non-free providers"
        with c.app.test_request_context("/v1/chat/completions", method="POST", json={},
                                        headers={c.FREE_ONLY_HEADER: "true"}):
            r = c._route_completion(
                {"model": "cascade", "messages": [{"role": "user", "content": "hi"}]}, False)
        assert r[0] == "error", f"expected an error result, got {r!r}"
        assert r[2] == 503, f"expected HTTP 503, got {r[2]}"
        assert r[1]["error"]["code"] == "free_only_no_provider", f"wrong error code: {r[1]}"
    finally:
        c.PROVIDERS = saved


@test("A provider that just failed is not retried in the same request (4b)")
def test_failed_provider_not_retried():
    c = _load_cascade()

    class _Pool:
        def __init__(self, names):
            self.pools = {n: ["key1", "key2"] for n in names}   # two keys each
        def get_key(self, name):
            lst = self.pools.get(name, [])
            return lst[0] if lst else None
        def mark_rate_limited(self, *a, **k):
            pass

    class _Stats:
        def breaker_open(self, name): return False
        def health_bucket(self, name): return 0
        def record_error(self, *a, **k): pass
        def record_health(self, *a, **k): pass
        def record_success(self, *a, **k): pass
        def record_trace(self, *a, **k): pass
        def record_cost(self, *a, **k): pass

    class _Bulkhead:
        def try_acquire(self, name): return True
        def release(self, name): pass

    class _Cache:
        def get(self, payload): return None
        def set(self, payload, data): pass

    calls = []
    def _fail_forward(provider, key, payload, streaming):
        calls.append((provider["name"], key))
        return (None, False)              # network failure on every attempt

    saved = (c.PROVIDERS, c.pool, c.stats, c.bulkhead, c.cache, c.forward)
    try:
        c.PROVIDERS = [{"name": "flaky", "model": "gemini-2.5-flash-lite", "cost": 0}]
        c.pool, c.stats, c.bulkhead, c.cache = _Pool(["flaky"]), _Stats(), _Bulkhead(), _Cache()
        c.forward = _fail_forward
        with c.app.test_request_context("/v1/chat/completions", method="POST", json={}):
            kind, body, status = c._route_completion(
                {"model": "cascade", "messages": [{"role": "user", "content": "hi"}]}, False)

        assert kind == "error" and status == 503, f"expected 503 error, got {kind}/{status}"
        # Two keys are configured; a failing provider must be tried exactly once
        # (old behaviour looped over both keys before cascading).
        assert len(calls) == 1, \
            f"failed provider was retried in the same request: {len(calls)} attempts {calls}"
    finally:
        (c.PROVIDERS, c.pool, c.stats, c.bulkhead, c.cache, c.forward) = saved


@test("Free-only OFF restores previous behaviour unchanged (4c)")
def test_free_only_off_unchanged():
    c = _load_cascade()
    saved = c.PROVIDERS
    try:
        c.PROVIDERS = _FO_PROVIDERS
        payload = {"model": "cascade", "messages": [{"role": "user", "content": "hi"}]}

        # No filtering whatsoever when the flag is off.
        off = [p["name"] for p in c._ordered_providers(payload, free_only=False)]
        assert set(off) == {p["name"] for p in _FO_PROVIDERS}, \
            f"free-only OFF filtered providers: {off}"

        # Opt-in detection is off by default...
        with c.app.test_request_context("/", method="POST", json={}):
            assert c._free_only_requested({"model": "cascade"}) is False, \
                "free-only requested without header or alias"
        # ...and turns on via the header (case/whitespace tolerant)...
        with c.app.test_request_context("/", method="POST", json={},
                                        headers={c.FREE_ONLY_HEADER: "  YES "}):
            assert c._free_only_requested({"model": "cascade"}) is True, \
                "free-only header not honoured"
        # ...or via the cascade-free alias.
        with c.app.test_request_context("/", method="POST", json={}):
            assert c._free_only_requested({"model": "cascade-free"}) is True, \
                "cascade-free alias not honoured"
    finally:
        c.PROVIDERS = saved


@test("Free-only pricing gate denies unpriced models and keeps free ones")
def test_model_is_free_gate():
    c = _load_cascade()
    assert c._model_is_free("gemini-2.5-flash-lite") is True, "known free model rejected"
    assert c._model_is_free("anthropic/claude-sonnet-5") is False, "known paid model accepted"
    assert c._model_is_free("totally-unpriced-model-xyz") is False, \
        "unpriced model treated as free (leak)"
    assert c._model_is_free("") is False, "empty model treated as free (leak)"
    # Dual gate: a paid TIER provider must not pass even with a free-priced model.
    assert c._provider_is_free({"name": "x", "model": "gpt-4o-mini", "cost": 1}) is False, \
        "paid-tier provider with a free-priced model passed the gate"
    assert c._provider_is_free({"name": "x", "model": "gemini-2.5-flash-lite", "cost": 0}) is True, \
        "genuinely free provider rejected"


@test("/v1/embeddings honours free-only (leak path d)")
def test_embeddings_free_only():
    c = _load_cascade()
    saved = c.PROVIDERS
    try:
        c.PROVIDERS = [
            {"name": "embed_free", "model": "gemini-2.5-flash-lite",
             "embed_model": "gemini-embedding-001", "cost": 0},
            {"name": "embed_paid", "model": "gpt-4o-mini",
             "embed_model": "text-embedding-3-small", "cost": 1},
        ]
        off = [p["name"] for p in c._embed_ordered(free_only=False)]
        assert set(off) == {"embed_free", "embed_paid"}, \
            f"free-only OFF changed the embedding provider list: {off}"
        free = [p["name"] for p in c._embed_ordered(free_only=True)]
        assert free == ["embed_free"], \
            f"paid embedding provider leaked under free-only: {free}"
    finally:
        c.PROVIDERS = saved


# ── Direct function registration ─────────────────────────────────────────────
# Register each test function


def _register_test(fn, name):
    """Register a function as a test."""
    setattr(fn, "__test_function__", True)
    return fn

# Discover all test_ functions and register them
_test_registry = []
for _name in list(globals().keys()):
    if _name.startswith("test_") and callable(globals()[_name]):
        _fn = globals()[_name]
        if not hasattr(_fn, "__test_function__"):
            globals()[_name] = _register_test(_fn, _name)

if __name__ == "__main__":
    # Actually just run them directly — simpler
    print("═══ Cascade Router Test Suite ═══")
    print(f"Target: {BASE}")
    print()

    # Health check
    try:
        code = _health()
        assert code == 200, f"health check failed: {code}"
        print(f"✓ Server reachable ({code})\n")
    except Exception as e:
        print(f"✗ Server unreachable: {e}")
        print("  Start cascade with: cd ~/.local/share/cascade && PROXY_API_KEYS=sk-router-1 venv/Scripts/python cascade.py")
        sys.exit(1)

    # Run tests in order
    all_tests = [
        ("Health", [
            test_health,
        ]),
        ("Basic Routing", [
            test_basic_chat,
            test_response_fields,
            test_multi_turn,
        ]),
        ("Trace IDs", [
            test_trace_id_present,
            test_trace_id_unique,
            test_trace_id_status,
        ]),
        ("Cost Tracking", [
            test_cost_tracking_status,
            test_cost_tracking_accumulates,
            test_cost_estimate,
        ]),
        ("Bulkheads", [
            test_bulkhead_enabled,
            test_bulkhead_active,
        ]),
        ("Provider Filtering", [
            test_provider_availability,
            test_at_least_one_available,
        ]),
        ("Model Pinning", [
            test_model_pinning,
            test_deepseek_routing,
        ]),
        ("Adaptive max_tokens", [
            test_adaptive_max_tokens,
        ]),
        ("Cache", [
            test_cache_status,
            test_cache_hits,
        ]),
        ("Fast Routing", [
            test_fast_routing_status,
        ]),
        ("Error Handling", [
            test_invalid_auth,
        ]),
        ("Status Endpoint", [
            test_status_sections,
            test_provider_stats_fields,
            test_status_latency,
        ]),
        ("Metrics", [
            test_metrics,
        ]),
        ("Free-Only Mode (unit)", [
            test_free_only_on_excludes_paid,
            test_free_only_fails_closed,
            test_failed_provider_not_retried,
            test_free_only_off_unchanged,
            test_model_is_free_gate,
            test_embeddings_free_only,
        ]),
    ]

    for group_name, tests in all_tests:
        print(f"── {group_name} ──")
        for test_fn in tests:
            try:
                test_fn()
            except Exception as e:
                print(f"  ✗ {test_fn.__name__}: {e}")
                FAIL += 1

    total = PASS + FAIL + SKIP
    print()
    print(f"═══ Results: {PASS} ✓ passed, {FAIL} ✗ failed, {SKIP} ⊘ skipped ({total} total) ═══")
    sys.exit(0 if FAIL == 0 else 1)
