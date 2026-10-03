import importlib.util
import itertools
import os
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests


@pytest.fixture
def router(monkeypatch, tmp_path, request):
    effects = []

    def deny_network(*args, **kwargs):
        raise AssertionError("offline test attempted network I/O")

    # Guard both the usual request entry point and direct Session.send calls.
    monkeypatch.setattr(requests.sessions.Session, "request", deny_network)
    monkeypatch.setattr(requests.sessions.Session, "send", deny_network)
    env = {
        "CASCADE_OFFLINE": "1",
        "CASCADE_API_KEY": "test-proxy-key",
        "OPENROUTER_API_KEYS": "test-only-openrouter-key",
        "CASCADE_ROUTE_LOG": str(tmp_path / "routes.jsonl"),
        # Windows Path.home() requires USERPROFILE even when HERMES_HOME is set.
        "USERPROFILE": str(tmp_path),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path),
    }
    if getattr(request, "param", True):
        env["BWS_ACCESS_TOKEN"] = "test-only-not-a-real-secret"
    monkeypatch.setattr(os, "environ", env)
    path = Path(__file__).resolve().parents[1] / "cascade.py"
    spec = importlib.util.spec_from_file_location("cascade_under_test", path)
    module = importlib.util.module_from_spec(spec)

    def exists(candidate):
        effects.append(("filesystem_probe", str(candidate)))
        return False

    def subprocess_run(*args, **kwargs):
        effects.append(("subprocess", "attempted"))
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "exists", exists)
        guard.setattr(shutil, "which", lambda name: "test-only-bws")
        guard.setattr(subprocess, "run", subprocess_run)
        guard.setattr(threading.Thread, "start",
                      lambda self: effects.append(("thread", "attempted")))
        spec.loader.exec_module(module)

    module._import_effects = effects
    module.__dict__["_import_providers"] = module.PROVIDERS
    module._ENCODER = None
    module.PROVIDERS = []
    module._provider_state = {}
    module._rr_counter = itertools.count()
    module.PROMPT_ROUTE_RULES = []
    module.CACHE_TTL = 600
    module.cache = module.ResponseCache(ttl=600, max_size=20)
    module.CASCADE_API_KEY = ["test-proxy-key"]
    return module
