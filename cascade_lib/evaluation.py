"""Offline summaries of complete model/topology evaluation receipts."""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def _number(value, field, *, integer=False):
    if type(value) not in (int, float):
        raise ValueError(f"{field} must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    if integer and not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def summarize(rows):
    """Group complete synthetic-or-real receipts by config; never infer missing cost."""
    groups = defaultdict(list)
    seen = set()
    for row in rows:
        if type(row) is not dict:
            raise ValueError("receipt must be an object")
        for field in ("config", "case"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"{field} must be a nonempty string")
        repeat = _number(row.get("repeat"), "repeat", integer=True)
        if repeat < 1:
            raise ValueError("repeat must be positive")
        key = row["config"], row["case"], repeat
        if key in seen:
            raise ValueError("duplicate case/config/repeat")
        seen.add(key)
        if type(row.get("accepted")) is not bool:
            raise ValueError("accepted must be boolean")
        for field in ("input_tokens", "output_tokens"):
            _number(row.get(field), field, integer=True)
        _number(row.get("elapsed_s"), "elapsed_s")
        if row.get("cost_usd") is not None:
            _number(row["cost_usd"], "cost_usd")
        groups[row["config"]].append(row)

    if not groups:
        raise ValueError("no receipts")
    result = []
    for config, items in sorted(groups.items()):
        accepted = sum(item["accepted"] for item in items)
        tokens = sum(item["input_tokens"] + item["output_tokens"] for item in items)
        known_cost = all(item.get("cost_usd") is not None for item in items)
        total_cost = sum(item["cost_usd"] for item in items) if known_cost else None
        result.append({
            "config": config,
            "attempted": len(items),
            "accepted": accepted,
            "acceptance_rate": accepted / len(items),
            "cases": sorted({item["case"] for item in items}),
            "total_tokens": tokens,
            "tokens_per_accepted": tokens / accepted if accepted else None,
            "elapsed_s": sum(item["elapsed_s"] for item in items),
            "cost_known": known_cost,
            "total_cost_usd": total_cost,
            "cost_per_accepted_usd": total_cost / accepted
            if total_cost is not None and accepted else None,
        })
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", type=Path)
    args = parser.parse_args(argv)
    try:
        rows = [json.loads(line) for line in args.receipts.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        result = summarize(rows)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"evaluation error: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
