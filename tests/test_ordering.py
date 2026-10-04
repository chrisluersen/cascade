def test_capable_paid_candidate_precedes_weak_free_candidate(router):
    weak = {"name": "weak", "model": "weak-test", "cost": 0}
    capable = {"name": "capable", "model": "capable-test", "cost": 1}
    router._provider_state = {
        "weak": {"rating": 5, "available": True, "latency_ms": 1},
        "capable": {"rating": 1, "available": True, "latency_ms": 10},
    }
    ordered = router._get_smart_ordered([weak, capable], complexity=1)
    assert [p["name"] for p in ordered] == ["capable", "weak"]


def test_cheaper_candidate_wins_within_capable_tier(router):
    cheap = {"name": "cheap", "model": "cheap-test", "cost": 0}
    paid = {"name": "paid", "model": "paid-test", "cost": 2}
    router._provider_state = {
        "cheap": {"rating": 1}, "paid": {"rating": 1},
    }
    ordered = router._get_smart_ordered([paid, cheap], complexity=2)
    assert ordered[0]["name"] == "cheap"


def test_free_only_still_excludes_paid_capable_candidate(router):
    router.PROVIDERS = [
        {"name": "free", "model": "glm-4.5-flash", "cost": 0},
        {"name": "paid", "model": "anthropic/claude-sonnet-5", "cost": 2},
    ]
    router._provider_state = {"free": {"rating": 5}, "paid": {"rating": 1}}
    payload = {"model": "cascade", "messages": [{"role": "user", "content": "design code"}]}
    assert [p["name"] for p in router._ordered_providers(payload, free_only=True)] == ["free"]
