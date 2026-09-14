# Free-only mode — implementation notes

**Branch:** `feat/free-only-mode` — **MERGED TO `main` 2026-09-13.**
*(This line previously read "not merged to `main`, not pushed." That was falsified: the
branch was already on `origin/main` as `aed90bd`; only the local `main` pointer was behind.
Merged as a fast-forward, restarted, and verified live — see "Verified" below. Rule 24: the
original claim is corrected in place, not silently rewritten.)*

## Status: COMPLETE — implemented, tested, committed, merged, and verified live

Originally parked as WIP (cut off at the 20-iteration leaf budget). The gaps
recorded below have since been closed and covered by tests.

> ~~Measure the diff with `git diff main feat/free-only-mode --ignore-cr-at-eol`.
> A naive `git diff` reports thousands of changed lines; that is CRLF churn, not code.~~
>
> **CORRECTED 2026-09-13 — this was wrong.** Both sides are **100% CRLF**
> (`main` = 2832/2832 lines, branch = 3012/3012), so there is no CR difference for
> `--ignore-cr-at-eol` to ignore: the naive diff **is** the real diff. The 5,844-line
> `cascade.py` delta is genuine code, and the flag gave false comfort. Measure with
> `git diff --stat`, not with a flag that assumes a cause you have not checked.

## Feature

Opt-in **per request** (no persistent state) via either:

- header `X-Cascade-Free-Only: true`
- model alias `"cascade-free"`

When enabled the router (a) never selects a paid model/provider and (b) never
retries a provider that already failed in the same request. With neither
opt-in present, behaviour is exactly as before and the endpoint stays
paid-capable — that is the design constraint and it is covered by test 4c.

## Implementation

- `FREE_ONLY_HEADER`, `FREE_ONLY_ALIAS`, `_free_only_requested()`.
- `_model_is_free()` — **deny-by-default**: only a model with an explicit
  `(0.0, 0.0)` pricing-table entry is free. An unpriced model is PAID, so it
  cannot leak (this replaces the old `unpriced → free-safe` default, which was
  itself a leak).
- `_provider_is_free(provider, model_key="model")` — requires **both**
  `cost == 0` and a free price for the selected model, so one weak signal can't
  admit a mislabelled provider. `model_key="embed_model"` is used for
  `/v1/embeddings`, whose cost follows a different model id.
- `_ordered_providers(payload, free_only=False)` — applies the free filter to
  the **model-pin branch as well as** the final ordering, so a request naming a
  paid model explicitly cannot slip past the gate.
- `_route_completion()` — resolves `free_only` once, normalises the
  `cascade-free` alias to `CASCADE_MODEL`, suppresses a paid prompt-route pin,
  and **fails closed** with `free_only_no_provider` (HTTP 503) when the
  free-filtered candidate list is empty.
- Failed-provider exclusion — a request-scoped `failed_providers` set: a
  provider that fails (network error, 5xx, unexpected non-2xx, 400/401/403,
  413) is not retried with its remaining keys in the same request. A 429 is
  *key* cooldown, not a provider failure, so key rotation is preserved.
  Applied to both the chat walk loop and the embeddings walk loop.
- `/v1/embeddings` — now reads the free-only opt-in and filters on the embed
  model; previously it ignored the flag entirely.

## Leak paths — all closed

| # | Path | State |
|---|---|---|
| a | Prompt-route keyword pin → paid model | Closed — pin suppressed under free-only |
| b | `_ordered_providers` model-pin branch with an explicit paid model | Closed — pin branch is free-filtered |
| c | Provider `cost == 0` but priced resolved model | Closed — dual check |
| d | `/v1/embeddings` ignoring the free-only flag | Closed — flag read + embed-model filter |
| e | Unpriced model treated as free | Closed — deny-by-default |

## Tests

`test_cascade.py` → group **"Free-Only Mode (unit)"** (no server required; it
imports `cascade.py` in-process so it verifies this branch's code rather than
whatever the live server is running):

- 4a free-only ON excludes every paid provider from the candidate list
- 4b a provider that just failed is not retried in the same request
- 4c free-only OFF restores the previous behaviour unchanged
- plus the fail-closed 503 gate, the unpriced/dual-gate pricing check, and the
  `/v1/embeddings` leak.

Run: `python test_cascade.py`. The unit group passes regardless of the live server.

> ~~Run: `python test_cascade.py` (set `CASCADE_AUTH_KEY` for the integration groups)~~
> **CORRECTED 2026-09-13:** the integration groups need **`CASCADE_API_KEY`**, which is
> what the probe, the watchdog and the host environment all use. The suite read only
> `CASCADE_AUTH_KEY` with a stale default (`sk-router-1`), so every integration test sent
> the wrong key and the suite produced **23 failures that were all one HTTP 401**. The
> file now resolves `CASCADE_API_KEY` first, falling back to `CASCADE_AUTH_KEY`, then `.env`.

## Verified 2026-09-13 (merged to `main`)

- **Merged as a fast-forward** (`main` was a strict ancestor — 0 commits in `main`
  not in the branch), so no conflict and no merge commit.
- **Restarted on `main`**: new pid, exactly **one** listener, `127.0.0.1:8319` only.
- **Live free-only acceptance 4/4** (`scripts/g4_acceptance.py`): the `cascade-free`
  alias serves a completion; it is served by a **free** model (`glm-4.5-flash`); the
  header form serves free; and a request naming an **explicit paid model** under
  free-only is substituted off the paid path.
- **Test suite: 27 passed / 6 failed — NOT the "33 tests pass" the gate recorded.**

## Two defects found while verifying (both fixed)

1. **The suite could not fail.** The `@test(name)` decorator's inner wrapper caught
   `AssertionError` and incremented a counter instead of raising, and pytest collects
   those wrappers as tests. Injecting an unconditional `assert False` still produced
   **"33 passed"**. Proven before/after by `scratch/falsify-pytest-teeth.py`. Fixed:
   the factory is marked `__test__ = False` and a failure now re-raises under pytest
   while the hand-rolled runner keeps tallying. **Consequence:** the gate's
   "33 tests pass" line was satisfied by a suite that was failing.
2. **The auth key was never right.** See the test-run correction above.

## The 6 remaining failures (classified, not hidden)

| Test | Cause |
|---|---|
| `test_deepseek_routing` (503) | **Environmental — cannot pass on this host.** Every paid provider here fails on balance, so there is no funded deepseek route to serve. |
| `test_model_pinning` | Same class as above — pins a paid model. |
| `test_streaming` | `JSONDecodeError` — the test expects a JSON body where the router may return an SSE stream. Test/contract mismatch, unclassified. |
| `test_cost_tracking_accumulates` | Assertion on missing provider/cost accounting. Unclassified. |
| `test_trace_id_unique`, `test_adaptive_max_tokens`, `test_cache_hits` | **Flaky, load-dependent 120 s timeouts** — which of them fails differs between runs. Not deterministic failures. |

## Notes

- ~~The production router serves `main`, so the live integration groups exercise
  `main`, not this branch. Verifying this branch end-to-end against HTTP would
  require restarting the service — deliberately not done here.~~
  **DONE 2026-09-13:** the service was restarted on the merged `main`, and the
  integration groups now exercise this code. That restart is what made the 401
  discovery possible.
- **`scripts/cascade-up.sh` had a silent no-op in its restart path** (found while
  doing that restart): it killed strays with `taskkill //F //PID`, which MSYS passes
  through literally so taskkill rejects it — and `|| true` swallowed the error. Fixed
  to `cmd.exe /c "taskkill /F /PID $STRAY"`, and it now **verifies the port is free
  and refuses to launch** rather than racing a listener that still holds it.
