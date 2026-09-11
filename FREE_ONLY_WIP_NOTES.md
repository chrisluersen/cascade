# Free-only mode — implementation notes

**Branch:** `feat/free-only-mode` · not merged to `main`, not pushed.

## Status: COMPLETE — implemented, tested, committed on this branch

Originally parked as WIP (cut off at the 20-iteration leaf budget). The gaps
recorded below have since been closed and covered by tests.

> Measure the diff with `git diff main feat/free-only-mode --ignore-cr-at-eol`.
> A naive `git diff` reports thousands of changed lines; that is CRLF churn, not code.

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

Run: `python test_cascade.py` (set `CASCADE_AUTH_KEY` for the integration
groups). The unit group passes regardless of the live server.

## Notes

- The production router serves `main`, so the live integration groups exercise
  `main`, not this branch. Verifying this branch end-to-end against HTTP would
  require restarting the service — deliberately not done here.
