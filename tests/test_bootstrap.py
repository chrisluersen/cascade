import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("router", [True, False], indirect=True, ids=["bws-token", "no-bws-token"])
def test_offline_import_has_no_startup_io(router, monkeypatch):
    assert router._import_effects == []
    assert router.OFFLINE is True
    assert router._AUTH_KEYS == {}
    assert router._BW_KEYS == {}
    assert router._keys_for("openrouter", "OPENROUTER_API_KEYS") == ["test-only-openrouter-key"]
    assert router._import_providers == []
    assert router.PROVIDERS == []

    def deny_download(*args, **kwargs):
        raise AssertionError("offline test attempted tokenizer download")

    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=deny_download))
    router._ENCODER = "uninitialized"
    assert router._get_encoder() is None
    assert router._ENCODER == "uninitialized"
