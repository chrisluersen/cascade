"""Response cache for cascade — deduplicates identical requests.

Thread-safe LRU cache with TTL. Uses SHA256 hashing of the JSON-serialised
request payload (minus ``stream``) for deterministic cache keys.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any


class ResponseCache:
    """Thread-safe LRU cache with per-entry TTL for cascade responses.

    Keys are deterministic SHA256 hashes of the request payload (minus the
    ``stream`` field, which doesn't change the answer).
    """

    def __init__(self, ttl: int = 300, max_size: int = 100):
        self.ttl = ttl
        self.max_size = max_size
        self.lock = threading.Lock()
        self._store: OrderedDict = OrderedDict()
        self.hits = 0
        self.misses = 0

    # ── Internal ─────────────────────────────────────────────────────────────

    @staticmethod
    def _hash(payload: dict) -> str:
        """Deterministic SHA256 hash of the request payload.

        Removes the ``stream`` field before hashing since it doesn't change
        the response content. Sorts keys for deterministic serialisation.
        """
        relevant = {k: v for k, v in payload.items() if k != "stream"}
        content = json.dumps(relevant, sort_keys=True, default=str)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    # ── Public API ───────────────────────────────────────────────────────────

    def get(self, payload: dict) -> dict | None:
        """Look up a cached response for the given payload.

        Returns the cached dict if found and not expired, else None.
        """
        if self.ttl <= 0:
            return None
        key = self._hash(payload)
        with self.lock:
            if key in self._store:
                data, ts = self._store[key]
                if time.time() - ts < self.ttl:
                    self._store.move_to_end(key)
                    self.hits += 1
                    return data
                del self._store[key]
            self.misses += 1
        return None

    def set(self, payload: dict, data: dict) -> None:
        """Store a response in the cache."""
        if self.ttl <= 0:
            return
        key = self._hash(payload)
        with self.lock:
            if len(self._store) >= self.max_size:
                self._store.popitem(last=False)
            self._store[key] = (data, time.time())

    @property
    def size(self) -> int:
        with self.lock:
            return len(self._store)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 3) if total else 0.0
