"""cascade_lib — modular library for the cascade AI model proxy.

Extracted from cascade.py for cleaner separation of concerns.
Version: 2.0.0 (module refactoring)
"""
from __future__ import annotations

from cascade_lib.auth import auth_check, get_bw_key, get_keys_for, init_auth, set_bw_aliases
from cascade_lib.cache import ResponseCache

__all__ = [
    "auth_check",
    "get_bw_key",
    "get_keys_for",
    "init_auth",
    "set_bw_aliases",
    "ResponseCache",
]
