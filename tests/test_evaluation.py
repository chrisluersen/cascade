"""Synthetic receipt fixtures for offline evaluator contract tests."""

import importlib.util
import json
import subprocess
import sys

import pytest


def test_evaluation_module_exists():
    assert importlib.util.find_spec("cascade_lib.evaluation") is not None


def receipt(case="a", accepted=True, cost=1.0):
    """A synthetic complete-run fixture, not a measured benchmark."""
    return {
        "config": "sol-opus", "case": case, "repeat": 1,
        "accepted": accepted, "input_tokens": 100, "output_tokens": 20,
        "elapsed_s": 10.0, "cost_usd": cost,
    }


def test_cost_per_accept_includes_failed_cases():
    from cascade_lib import evaluation

    assert hasattr(evaluation, "summarize"), "summarize is not implemented"
    result = evaluation.summarize([receipt(), receipt("b", False, 2.0)])[0]
    assert result["accepted"] == 1
    assert result["attempted"] == 2
    assert result["cost_per_accepted_usd"] == 3.0
    assert result["tokens_per_accepted"] == 240


def test_unknown_cost_is_not_zero():
    from cascade_lib.evaluation import summarize

    result = summarize([receipt(cost=None)])[0]
    assert result["cost_known"] is False
    assert result["total_cost_usd"] is None
    assert result["cost_per_accepted_usd"] is None


def test_no_accepted_cases_has_no_ratio():
    from cascade_lib.evaluation import summarize

    result = summarize([receipt(accepted=False)])[0]
    assert result["cost_per_accepted_usd"] is None
    assert result["tokens_per_accepted"] is None


@pytest.mark.parametrize("field,value", [
    ("cost_usd", True), ("cost_usd", -1), ("cost_usd", float("nan")),
    ("cost_usd", float("inf")), ("cost_usd", float("-inf")),
    ("input_tokens", True), ("output_tokens", 1.5),
    ("elapsed_s", float("inf")), ("accepted", "true"), ("repeat", 0),
])
def test_invalid_receipt_is_rejected(field, value):
    from cascade_lib.evaluation import summarize

    row = receipt()
    row[field] = value
    with pytest.raises(ValueError):
        summarize([row])


@pytest.mark.parametrize("rows", [[], [{}], [None], [receipt(cost="invalid")]])
def test_empty_or_invalid_receipts_cannot_win(rows):
    from cascade_lib.evaluation import summarize

    with pytest.raises(ValueError):
        summarize(rows)


def test_duplicate_receipt_is_rejected():
    from cascade_lib.evaluation import summarize

    with pytest.raises(ValueError, match="duplicate"):
        summarize([receipt(), receipt()])


def test_cli_prints_synthetic_fixture_summary(tmp_path):
    path = tmp_path / "synthetic-receipts.jsonl"
    path.write_text(json.dumps(receipt()) + "\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "cascade_lib.evaluation", str(path)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary[0]["cost_per_accepted_usd"] == 1.0
    assert summary[0]["attempted"] == 1


@pytest.mark.parametrize("contents", ["", "not-json\n", "{}\n"])
def test_cli_rejects_empty_or_malformed_synthetic_fixture(tmp_path, contents):
    path = tmp_path / "invalid-synthetic-receipts.jsonl"
    path.write_text(contents, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "cascade_lib.evaluation", str(path)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "evaluation error:" in result.stderr
