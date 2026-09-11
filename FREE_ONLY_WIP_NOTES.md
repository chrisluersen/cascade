# Free-only mode — WIP notes (incomplete)

**Branch:** `feat/free-only-mode` · **DO NOT MERGE** · cut off at the 20-iteration leaf budget.

## Verified state of this branch (checked by the parent, not self-reported)

| Check | Result |
|---|---|
| `py_compile cascade.py` | **OK** — parses |
| Stale `_provider_is_free_safe` reference | **absent** (reverted cleanly) |
| Real diff vs `main` | **101 insertions, 0 deletions** |

> Measure the diff with `git diff main feat/free-only-mode --ignore-cr-at-eol`.
> A naive `git diff` reports ~5,700 changed lines; that is CRLF churn, not code.

## What IS done

- `FREE_ONLY_HEADER = "X-Cascade-Free-Only"`, `FREE_ONLY_ALIAS = "cascade-free"`.
- `_model_is_free()` / `_provider_is_free()` / `_free_only_requested()`.
  `_provider_is_free()` deliberately requires **both** `cost == 0` **and** a free price,
  so a mislabelled provider cannot leak through one weak signal.
- `_route_completion` resolves `free_only` per request, normalises the `cascade-free`
  alias to `CASCADE_MODEL`, suppresses the paid prompt-route pin, and installs a
  post-ordering free filter with a `free_only_no_provider` 503.

## What is NOT done

1. **Failed-provider exclusion is not implemented.** Requirement 3 is entirely absent —
   a provider that fails can still be retried within the same request.
2. **No tests.** Requirement 4 (the three cases in `test_cascade.py`) does not exist.
3. **The suite was never run.** No verbatim output exists; nothing is verified end-to-end.

## Paid-leak paths still open (confirmed against the file, not assumed)

| # | Path | State |
|---|---|---|
| a | Prompt-route keyword pin → `anthropic/claude-sonnet-5` | Partially handled (pin suppressed) |
| b | `_ordered_providers` model-pin branch when `payload["model"]` names a paid model explicitly | **OPEN** — not filtered |
| c | Provider with `cost == 0` but a priced resolved model | Handled by the dual check |
| d | **`/v1/embeddings` (line ~2697) never reads the free-only flag** | **OPEN — confirmed leak** |

## Known defect to fix while you are here

`_model_is_free()` returns `True` for a model with **no pricing-table entry**:

```python
return True   # unpriced → free-safe default
```

That is a leak: an unpriced paid model is treated as free. **Invert it — deny on unpriced.**

## How a caller enables it

```
curl http://127.0.0.1:8319/v1/chat/completions \
  -H "Authorization: Bearer $CASCADE_API_KEY" \
  -H "X-Cascade-Free-Only: true" \
  -d '{"model":"cascade","messages":[{"role":"user","content":"hi"}]}'
```

Or send `"model": "cascade-free"`. With neither, behaviour is unchanged and the
endpoint stays paid-capable — that is the design constraint; do not break it.

## Resume order

1. Re-read `cascade.py` ~2440–2660 and confirm consistency.
2. Finish the walk-loop failed-provider exclusion.
3. Close leak paths (b) and (d), and invert the unpriced default.
4. Add tests 4a / 4b / 4c; restart the server; run `python test_cascade.py`; capture output.
5. Commit here. **Do not push. Do not touch the root Hermes config.**
