from types import SimpleNamespace

import requests


def test_anthropic_forward_returns_response_and_clamp_flag(router, monkeypatch):
    response = SimpleNamespace(status_code=200)
    monkeypatch.setattr(router._HTTP, "post", lambda *a, **kw: response)
    result = router.forward(
        {"name": "anthropic", "protocol": "anthropic", "model": "test-model"},
        "test-key", {"model": "cascade", "messages": [], "max_tokens": 10}, False,
    )
    assert result == (response, False)


def test_anthropic_network_failure_preserves_return_shape(router, monkeypatch):
    def timeout(*args, **kwargs):
        raise requests.exceptions.Timeout("synthetic timeout")

    monkeypatch.setattr(router._HTTP, "post", timeout)
    result = router.forward(
        {"name": "anthropic", "protocol": "anthropic", "model": "test-model"},
        "test-key", {"model": "cascade", "messages": [], "max_tokens": 10}, False,
    )
    assert result == (None, False)
