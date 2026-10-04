"""Provider-state freshness.

Regression: a 24h TTL combined with the skip-probe guard meant a single transient
probe failure marked a live provider unavailable for a full day. Measured on
2026-10-04: cascade_state.json said huggingface available=False while the same
endpoint returned HTTP 200, and the state was only ~3h old (inside the TTL).
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = (REPO / "cascade.py").read_text(encoding="utf-8")


def test_state_ttl_is_overridable_via_env():
    assert "CASCADE_STATE_TTL_HOURS" in SOURCE, (
        "state TTL must be operator-overridable so a bad state can be refreshed "
        "without editing code"
    )


def test_state_ttl_default_is_short_enough_to_self_heal():
    m = re.search(r'CASCADE_STATE_TTL_HOURS",\s*(\d+)\)', SOURCE)
    assert m, "STATE_TTL_HOURS default integer not found in cascade.py"
    hours = int(m.group(1))
    assert hours <= 6, (
        f"STATE_TTL_HOURS default is {hours}h — too long for a transient probe "
        "failure to self-heal within a working day"
    )