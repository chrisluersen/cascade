"""Authentication for cascade — Bitwarden, auth.json, and request auth check.

Loads API keys from multiple sources (Bitwarden Secrets Manager, auth.json,
and environment variables) and provides the ``_auth_check()`` Flask decorator
for route protection.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger("cascade")

# ── Auth file ─────────────────────────────────────────────────────────────────

AUTH_FILE: Path | None = None
_AUTH_KEYS: dict[str, list[str]] = {}
_BW_KEYS: dict[str, str] = {}
_BW_ENV_ALIASES: dict[str, str] = {}


def init_auth(
    auth_file: Path,
    bws_token: str = "",
    bw_aliases: dict[str, str] | None = None,
) -> None:
    """Initialise the auth subsystem.

    Call once at startup after config is loaded. Sets up auth.json loading,
    Bitwarden integration, and alias resolution.

    Args:
        auth_file: Path to the auth.json file.
        bws_token: Bitwarden Secrets Manager access token (empty = skip BW).
        bw_aliases: Optional mapping of cascade env var names → Bitwarden key names.
    """
    global AUTH_FILE, _AUTH_KEYS, _BW_KEYS, _BW_ENV_ALIASES
    AUTH_FILE = auth_file
    _AUTH_KEYS = _load_auth_json()
    _BW_KEYS = _load_bitwarden_keys(bws_token) if bws_token else {}
    if bw_aliases:
        _BW_ENV_ALIASES.update(bw_aliases)


def set_bw_aliases(aliases: dict[str, str]) -> None:
    """Update Bitwarden alias mappings after init (e.g. after config variables are defined)."""
    _BW_ENV_ALIASES.update(aliases)


def get_bw_key(key_name: str) -> str:
    """Look up a single key from the Bitwarden key cache.

    Returns the key value if found, or empty string if not.
    Useful for config values that may come from Bitwarden (e.g. OLLAMA_MODEL).
    """
    return _BW_KEYS.get(key_name, "")


def _load_auth_json() -> dict[str, list[str]]:
    """Load provider API keys from auth.json.

    Returns {provider_name: [keys]}. A missing or invalid file is non-fatal.
    """
    if not AUTH_FILE or not AUTH_FILE.exists():
        return {}
    try:
        doc = json.loads(AUTH_FILE.read_text())
        out: dict[str, list[str]] = {}
        for name, keys in doc.get("providers", {}).items():
            if isinstance(keys, list):
                out[name] = [str(k).strip() for k in keys if str(k).strip()]
        return out
    except Exception as e:
        log.warning("Could not read %s: %s", AUTH_FILE, e)
        return {}


def _load_bitwarden_keys(bws_token: str) -> dict[str, str]:
    """Fetch ALL secrets from Bitwarden Secrets Manager via the ``bws`` CLI.

    Args:
        bws_token: The BWS_ACCESS_TOKEN value.

    Returns {env_var_name: value} on success, {} on any failure.
    """
    if not bws_token:
        return {}

    bws_path = shutil.which("bws")
    if not bws_path:
        hermes_bin = os.path.expanduser("~/AppData/Local/hermes/bin/bws")
        if os.path.isfile(hermes_bin):
            bws_path = hermes_bin
    if not bws_path:
        log.warning("bws CLI not found — skipping Bitwarden key loading")
        return {}

    try:
        result = subprocess.run(
            [bws_path, "secret", "list"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "BWS_ACCESS_TOKEN": bws_token},
        )
        if result.returncode != 0:
            log.warning("bws secret list exited %d: %s", result.returncode, result.stderr.strip())
            return {}

        secrets = json.loads(result.stdout)
        if not isinstance(secrets, list):
            return {}

        out: dict[str, str] = {}
        for entry in secrets:
            key = (entry.get("key") or "").strip()
            value = (entry.get("value") or "").strip()
            if key and value:
                out[key] = value
        log.info("Loaded %d API keys from Bitwarden", len(out))
        return out
    except FileNotFoundError:
        log.warning("bws CLI not found — skipping Bitwarden key loading")
        return {}
    except subprocess.TimeoutExpired:
        log.warning("bws secret list timed out after 10s — skipping Bitwarden")
        return {}
    except json.JSONDecodeError as e:
        log.warning("bws output was not valid JSON: %s", e)
        return {}
    except Exception as e:
        log.warning("Failed to load keys from Bitwarden: %s", e)
        return {}


def get_keys_for(provider_name: str, env_var: str, env_fallback: bool = True) -> list[str]:
    """All keys for a provider: auth.json → Bitwarden → environment.

    Args:
        provider_name: The provider's internal name (e.g. ``"openai"``).
        env_var: The canonical env var name (e.g. ``"OPENAI_API_KEYS"``).
        env_fallback: If True, read from ``os.environ`` as final fallback.

    Returns deduplicated list of keys, order preserved.
    """
    merged: list[str] = []

    # 1. auth.json
    merged.extend(_AUTH_KEYS.get(provider_name, []))

    # 2. Bitwarden
    bw_val = _resolve_bw_key(env_var)
    if bw_val:
        merged.append(bw_val)

    # 3. Environment variables
    if env_fallback:
        merged.extend(_keys_from_env(env_var))

    seen: set[str] = set()
    out: list[str] = []
    for k in merged:
        if k and k not in seen and "«redacted" not in k:
            seen.add(k)
            out.append(k)
    return out


def _resolve_bw_key(env_var: str) -> str:
    """Resolve an env var name to a Bitwarden key through multiple strategies.

    1. Exact match
    2. Singular form (strip trailing S)
    3. Global alias table
    """
    val = _BW_KEYS.get(env_var, "")
    if val:
        return val
    singular = env_var[:-1] if env_var.endswith("S") else ""
    if singular and singular != env_var:
        val = _BW_KEYS.get(singular, "")
    if val:
        return val
    aliased = _BW_ENV_ALIASES.get(env_var, "")
    if aliased:
        val = _BW_KEYS.get(aliased, "")
    return val or ""


def _keys_from_env(env_var: str) -> list[str]:
    """Collect keys from environment using singular, plural, and numbered forms."""
    collected: list[str] = []
    singular = env_var[:-1] if env_var.endswith("S") else env_var
    if singular != env_var:
        single = os.environ.get(singular, "").strip()
        if single:
            collected.append(single)
    for piece in os.environ.get(env_var, "").split(","):
        piece = piece.strip()
        if piece:
            collected.append(piece)
    i = 2
    while True:
        nv = os.environ.get(f"{singular}_{i}", "").strip()
        if not nv:
            break
        collected.append(nv)
        i += 1
    seen: set[str] = set()
    out: list[str] = []
    for k in collected:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def auth_check(
    request: Any,
    valid_keys: list[str],
) -> Any | None:
    """Flask route guard — returns 401 JSON response or None if authenticated.

    Validates against:
    1. Explicit ``valid_keys`` list (from env var CASCADE_API_KEY)
    2. Bitwarden's CASCADE_API_KEY secret (loaded via init_auth)

    Args:
        request: Flask request object.
        valid_keys: List of valid API keys from environment.

    Returns:
        A Flask JSON response (401) if unauthorized, or None if auth passes.
    """
    from flask import jsonify

    header = request.headers.get("Authorization", "").strip()
    token = header[7:].strip() if header[:7].lower() == "bearer " else header
    if not token:
        token = request.headers.get("x-api-key", "").strip()

    all_valid = set(valid_keys)
    if _BW_KEYS:
        bw_key = _BW_KEYS.get("CASCADE_API_KEY", "")
        if bw_key:
            all_valid.add(bw_key)

    if not any(hmac.compare_digest(token, k) for k in all_valid):
        return jsonify({"error": "unauthorized"}), 401
    return None
