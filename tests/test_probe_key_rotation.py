"""The provider probe must try every key, not just the first.

Measured 2026-10-04: ``huggingface`` and ``groq`` were reported unavailable
because ``_initialize_ratings`` probed ``probe[0]["key"]`` — the stale Bitwarden
secret — and never reached the valid second key. The request path rotates on a
401, but the probe had already marked the provider down, so it was filtered out
before any request could reach it.
"""
from types import SimpleNamespace


def test_probe_tries_every_key_until_one_works(router, monkeypatch, tmp_path):
    router.STATE_FILE = tmp_path / "state.json"
    monkeypatch.setattr(router, "_probe_tools", lambda *a, **k: False)
    monkeypatch.setattr(router, "_probe_reasoning", lambda *a, **k: False)

    tried = []

    def fake_probe(provider, key):
        tried.append(key)
        return (key == "good"), 1.0, provider["model"]

    monkeypatch.setattr(router, "_probe_provider", fake_probe)

    provider = {"name": "shadowed", "base_url": "http://x/v1", "model": "m"}
    pool_ref = SimpleNamespace(pools={"shadowed": [{"key": "stale"}, {"key": "good"}]})

    router._initialize_ratings([provider], pool_ref)

    assert tried == ["stale", "good"], tried
    assert router._provider_state["shadowed"]["available"] is True


def test_probe_marks_provider_down_only_when_all_keys_fail(router, monkeypatch, tmp_path):
    router.STATE_FILE = tmp_path / "state.json"
    monkeypatch.setattr(router, "_probe_tools", lambda *a, **k: False)
    monkeypatch.setattr(router, "_probe_reasoning", lambda *a, **k: False)

    tried = []

    def fake_probe(provider, key):
        tried.append(key)
        return False, 1.0, provider["model"]

    monkeypatch.setattr(router, "_probe_provider", fake_probe)

    provider = {"name": "dead", "base_url": "http://x/v1", "model": "m"}
    pool_ref = SimpleNamespace(pools={"dead": [{"key": "k1"}, {"key": "k2"}]})

    router._initialize_ratings([provider], pool_ref)

    assert tried == ["k1", "k2"], tried
    assert router._provider_state["dead"]["available"] is False