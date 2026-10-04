from copy import deepcopy
from types import SimpleNamespace

import pytest


def configure(router, monkeypatch, providers):
    router.PROVIDERS = providers
    router.pool = router.CredentialPool(providers)
    router.bulkhead = router.BulkheadManager(providers)
    router.stats = router.ProviderStats()
    router._provider_state = {
        p["name"]: {"rating": 1, "available": True, "supports_tools": True}
        for p in providers
    }
    calls = []

    def forward(provider, key, payload, streaming):
        calls.append(provider["name"])
        data = {
            "id": "test-completion", "object": "chat.completion",
            "model": provider["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }
        return SimpleNamespace(status_code=200, json=lambda: deepcopy(data)), False

    monkeypatch.setattr(router, "forward", forward)
    return calls


def test_identical_request_hits_cache_before_payload_rewrites(router, monkeypatch):
    calls = configure(router, monkeypatch, [
        {"name": "test", "model": "test-model", "cost": 1, "keys": ["test-key"]}
    ])
    payload = {"model": "cascade", "messages": [{"role": "user", "content": "hi"}]}
    client = router.app.test_client()
    headers = {"Authorization": "Bearer test-proxy-key"}
    first = client.post("/v1/chat/completions", json=payload, headers=headers)
    second = client.post("/v1/chat/completions", json=payload, headers=headers)
    assert first.status_code == second.status_code == 200
    assert calls == ["test"]
    assert first.headers["X-Trace-Id"] != second.headers["X-Trace-Id"]


@pytest.mark.parametrize("endpoint", ["/v1/chat/completions", "/v1/embeddings"])
def test_free_only_does_not_reuse_unrestricted_cache(router, monkeypatch, endpoint):
    providers = [
        {"name": "paid", "model": "paid-model", "embed_model": "paid-embed",
         "cost": 1, "keys": ["test-paid"]},
        {"name": "free", "model": "glm-4.5-flash", "embed_model": "gemini-embedding-001",
         "cost": 0, "keys": ["test-free"]},
    ]
    calls = configure(router, monkeypatch, providers)
    monkeypatch.setattr(router, "_ordered_providers",
                        lambda payload, free_only=False: providers[1:] if free_only else providers)
    monkeypatch.setattr(router, "_embed_ordered",
                        lambda free_only=False: providers[1:] if free_only else providers)

    def embed(provider, key, payload):
        calls.append(provider["name"])
        return SimpleNamespace(status_code=200, json=lambda: {
            "object": "list", "model": provider["embed_model"],
            "data": [{"index": 0, "embedding": [1.0]}],
        })

    monkeypatch.setattr(router, "forward_embeddings", embed)
    payload = {"model": "cascade", "max_tokens": 10,
               "messages": [{"role": "user", "content": "hi"}], "input": "hi"}
    client = router.app.test_client()
    auth = {"Authorization": "Bearer test-proxy-key"}
    first = client.post(endpoint, json=payload, headers=auth)
    second = client.post(endpoint, json=payload,
                         headers={**auth, "X-Cascade-Free-Only": "true"})
    assert first.status_code == second.status_code == 200
    assert calls == ["paid", "free"]


def test_cache_identity_separates_endpoints_and_copies_input(router):
    payload = {"model": "cascade", "input": "hi", "messages": []}
    chat = router._cache_identity(payload, "chat", False)
    embed = router._cache_identity(payload, "embeddings", False)
    assert router.cache._hash(chat) != router.cache._hash(embed)
    payload["messages"].append({"role": "user", "content": "changed"})
    assert chat["payload"]["messages"] == []
