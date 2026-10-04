"""Key resolution must not let a stale secret shadow a good one.

Measured 2026-10-04: Bitwarden held BOTH a plural and a singular secret for two
providers, and the plural one was invalid:

    GROQ_API_KEYS         -> HTTP 401   (stale)
    GROQ_API_KEY          -> HTTP 200   (good)
    HUGGINGFACE_API_KEYS  -> HTTP 401   (stale)
    HF_TOKEN              -> HTTP 200   (good)

``_keys_for`` stopped at the FIRST resolution strategy that matched (exact name),
so cascade used the invalid key, got a 401, and dropped a provider that was
actually reachable. Both candidates must be returned so the key pool can rotate.
"""
import pytest


def _resolve(router, env_var, bw_keys):
    router._BW_KEYS = dict(bw_keys)
    router._AUTH_KEYS = {}
    return router._keys_for("testprov", env_var)


def test_exact_and_singular_bitwarden_keys_are_both_returned(router):
    keys = _resolve(router, "GROQ_API_KEYS",
                    {"GROQ_API_KEYS": "stale-bad", "GROQ_API_KEY": "good"})
    assert keys == ["stale-bad", "good"], keys


def test_alias_key_is_collected_alongside_the_exact_match(router):
    keys = _resolve(router, "HUGGINGFACE_API_KEYS",
                    {"HUGGINGFACE_API_KEYS": "stale-bad", "HF_TOKEN": "good"})
    assert keys == ["stale-bad", "good"], keys


def test_single_key_provider_is_unchanged(router):
    # Use an env var absent from the fixture's environment so only BW contributes.
    keys = _resolve(router, "MISTRAL_API_KEYS", {"MISTRAL_API_KEY": "only-good"})
    assert keys == ["only-good"], keys


def test_no_bitwarden_match_still_returns_env_keys(router, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEYS", "from-env")
    keys = _resolve(router, "OPENROUTER_API_KEYS", {})
    assert "from-env" in keys, keys