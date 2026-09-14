"""test_route_log.py — the route-log instrument writes what it claims to.

Exists because the FIRST version of this logger failed silently: it referenced an
undefined module alias, the `except Exception` swallowed the NameError, and the file
simply never appeared. That is indistinguishable from "no traffic arrived" -- the same
shape as a watchdog reporting ok while broken, which is the failure class this repo
keeps finding. A best-effort logger still needs a test that proves it wrote something.

Run: python test_route_log.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cascade  # noqa: E402

RECORD = dict(
    trace_id="abcdef123456",
    route="cascade-free-alias",
    outcome="ok",
    model="glm-4.5-flash",
    provider="zai",
    free_only=True,
    streaming=False,
    latency_ms=1234,
    cost_usd=0.0,
    prompt_tokens=10,
    completion_tokens=5,
)

results: list[tuple[str, bool, str]] = []


def check(label: str, cond: bool, why: str = "") -> None:
    results.append((label, cond, why))


# --- 1. it actually writes one parseable line ---------------------------------
with tempfile.TemporaryDirectory() as td:
    f = Path(td) / "cascade-routes.jsonl"
    cascade.ROUTE_LOG_PATH = str(f)
    cascade._route_log(**RECORD)
    ok = f.exists()
    check("writes a file", ok, f"exists={ok}")
    if ok:
        lines = f.read_text(encoding="utf-8").strip().splitlines()
        check("exactly one line", len(lines) == 1, f"lines={len(lines)}")
        rec = json.loads(lines[0])
        check("has a timestamp", bool(rec.get("ts")), f"ts={rec.get('ts')!r}")
        for k in ("route", "model", "provider", "outcome", "free_only"):
            check(f"carries {k}", k in rec, f"{k}={rec.get(k)!r}")
        # all records must share ONE schema so the log stays parseable as data
        check("cost survives at zero", rec.get("cost_usd") == 0.0, f"cost={rec.get('cost_usd')!r}")

    # --- 2. appends, does not truncate ---------------------------------------
    cascade._route_log(trace_id="second", route="direct", outcome="error")
    n = len(f.read_text(encoding="utf-8").strip().splitlines()) if f.exists() else 0
    check("appends (2 lines now)", n == 2, f"lines={n}")

    # --- 3. None-valued fields are dropped, not written as null --------------
    rec2 = json.loads(f.read_text(encoding="utf-8").strip().splitlines()[1])
    check("omits None fields", "cost_usd" not in rec2, f"keys={sorted(rec2)}")

# --- 4. a write failure must NOT raise (routing must survive) ----------------
cascade.ROUTE_LOG_PATH = str(Path(tempfile.gettempdir()) / "no" / "such" / "dir" / "x.jsonl")
try:
    cascade._route_log(**RECORD)
    check("unwritable path does not raise", True)
except Exception as e:  # noqa: BLE001
    check("unwritable path does not raise", False, f"raised {type(e).__name__}: {e}")

passed = sum(1 for _, ok, _ in results if ok)
for label, ok, why in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"\n          {why}" if not ok else ""))
print(f"\n{passed} passed, {len(results) - passed} failed")
sys.exit(0 if passed == len(results) else 1)
