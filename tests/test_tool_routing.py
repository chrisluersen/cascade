import pytest


@pytest.mark.parametrize("support", [False, None])
def test_tool_request_never_uses_unverified_provider(router, monkeypatch, support):
    provider = {"name": "test", "model": "test-model", "cost": 1, "keys": ["test-key"]}
    router.PROVIDERS = [provider]
    router.pool = router.CredentialPool([provider])
    router._provider_state = {"test": {"rating": 1, "available": True}}
    if support is not None:
        router._provider_state["test"]["supports_tools"] = support
    calls = []

    def forward(*args, **kwargs):
        calls.append("forwarded")
        return None, False

    monkeypatch.setattr(router, "forward", forward)
    payload = {
        "model": "cascade", "messages": [{"role": "user", "content": "lookup"}],
        "tools": [{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object", "properties": {}}
        }}],
    }
    response = router.app.test_client().post(
        "/v1/chat/completions", json=payload,
        headers={"Authorization": "Bearer test-proxy-key"},
    )
    assert calls == []
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "tool_capability_unavailable"


def test_verified_tool_support_is_accepted(router):
    provider = {"name": "test", "model": "test-model"}
    router._provider_state = {"test": {"supports_tools": True}}
    assert router._supports_tools(provider) is True
