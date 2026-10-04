import pytest


@pytest.mark.parametrize("model", ["vendor/glm-4.5-flash-paid", "gpt-4o-mini-new-paid"])
def test_unknown_model_is_not_known_free(router, model):
    assert router._model_is_free(model) is False
    assert router._model_is_priced(model) is False


def test_cost_estimate_does_not_borrow_a_substring_price(router):
    model = "vendor/anthropic/claude-sonnet-5-unverified"
    assert router._model_is_priced(model) is False
    assert router._estimate_cost(1_000_000, 1_000_000, model) == 0.0


def test_exact_known_price_still_estimates_cost(router):
    router.KNOWN_MODEL_COSTS = {"test-model": (2.0, 6.0)}
    assert router._model_is_priced("test-model") is True
    assert router._estimate_cost(1_000_000, 500_000, "test-model") == 5.0
