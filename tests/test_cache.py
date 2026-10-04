from cascade_lib.cache import ResponseCache


def test_router_uses_shared_cache(router):
    assert router.ResponseCache is ResponseCache


def test_updating_existing_entry_does_not_evict_other_entry():
    cache = ResponseCache(max_size=2)
    cache.set({"id": "a"}, {"value": 1})
    cache.set({"id": "b"}, {"value": 2})
    cache.set({"id": "b"}, {"value": 3})
    assert cache.get({"id": "a"}) == {"value": 1}
    assert cache.get({"id": "b"}) == {"value": 3}


def test_zero_capacity_disables_storage():
    cache = ResponseCache(max_size=0)
    cache.set({"id": "a"}, {"value": 1})
    assert cache.size == 0
    assert cache.get({"id": "a"}) is None


def test_cached_data_cannot_be_mutated_by_callers():
    cache = ResponseCache()
    payload = {"id": "a"}
    data = {"nested": {"value": 1}}
    cache.set(payload, data)
    data["nested"]["value"] = 2
    first = cache.get(payload)
    assert first == {"nested": {"value": 1}}
    first["nested"]["value"] = 3
    assert cache.get(payload) == {"nested": {"value": 1}}
