#!/usr/bin/env bash
#
# cascade restart — restart cascade so config/key changes take effect
#
# Use this after `cascade auth add` (or any .env change). It restarts cleanly:
#   • If a systemd service exists, it restarts that.
#   • Otherwise it finds the running cascade.py process, stops it, and relaunches
#     it in the background (logging to ./router.log).
# Either way it then health-checks cascade and tells you the result.
#
# Usage:
#   cascade restart
#
# Optional env overrides:
#   PORT=8319                      # port to health-check
#   PYTHON=python3                 # interpreter for the background fallback
#   CASCADE_SERVICE=my-svc        # systemd unit name (default: cascade)
#
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "cannot cd to repo dir"; exit 1; }
if [ -f "$REPO/venv/Scripts/python" ]; then
  PYTHON="${PYTHON:-$REPO/venv/Scripts/python}"
elif [ -f "$REPO/venv/bin/python" ]; then
  PYTHON="${PYTHON:-$REPO/venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi
PORT="${PORT:-8319}"
SERVICE="${CASCADE_SERVICE:-cascade}"

log() { printf '\033[1;36m[restart]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[restart]\033[0m %s\n' "$*" >&2; }
ok()  { printf '\033[1;32m[restart]\033[0m %s\n' "$*"; }

# Wait up to ~15s for /health to come back. 0=healthy, 1=not, 2=can't check.
health_ok() {
  command -v curl >/dev/null 2>&1 || return 2
  for _ in $(seq 1 15); do
    curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

report_health() {
  health_ok
  case $? in
    0) ok "cascade is healthy on :${PORT}.  (see:  cascade status)" ;;
    2) ok "restarted (install 'curl' to enable health checks)." ;;
    *) err "cascade did not come back healthy on :${PORT} — check the logs."; exit 1 ;;
  esac
}

# 1) systemd path -----------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1 \
   && systemctl cat "${SERVICE}.service" >/dev/null 2>&1; then
  log "restarting systemd service '${SERVICE}'…"
  if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
    sudo systemctl restart "$SERVICE" || { err "systemctl restart failed."; exit 1; }
  else
    systemctl restart "$SERVICE" || { err "systemctl restart failed."; exit 1; }
  fi
  report_health
  exit 0
fi

# 2) standalone process path ------------------------------------------------------
# On Windows (MSYS), pgrep doesn't find native processes — use netstat + tasklist instead
_find_cascade_pids_windows() {
  # Find the PID(s) LISTENING on our port — the actual router process.
  # On MSYS, established connections TO the router also match :PORT but belong
  # to the client process (Hermes). The LISTENING entry is the one true owner.
  # tasklist is intentionally NOT used here because it returns ALL python
  # processes (including Hermes itself) — killing those would corrupt the session.
  netstat -ano 2>/dev/null \
    | grep ":${PORT} " \
    | grep -i "LISTENING" \
    | awk '{print $NF}' \
    | sort -un \
    | tr '\n' ' '
}

# Find a running cascade.py belonging to this repo (best-effort).
# First try pgrep (Linux/macOS); on MSYS fall back to netstat
pids=""
if [[ "$(uname -s)" == MINGW* ]] || [[ "$(uname -s)" == MSYS* ]] || [[ "$(uname -s)" == CYGWIN* ]]; then
  pids=$(_find_cascade_pids_windows)
else
  pids=$(pgrep -f "${REPO}/cascade.py" 2>/dev/null || pgrep -f "cascade.py" 2>/dev/null || true)
fi
if [ -n "$pids" ]; then
  log "stopping running cascade (pid: $(echo "$pids" | tr '\n' ' '))…"
  # shellcheck disable=SC2086
  if [[ "$(uname -s)" == MINGW* ]] || [[ "$(uname -s)" == MSYS* ]] || [[ "$(uname -s)" == CYGWIN* ]]; then
    for pid in $pids; do
      taskkill //F //PID "$pid" 2>/dev/null || true
    done
  else
    kill $pids 2>/dev/null || true
  fi
  for _ in $(seq 1 10); do
    pgrep -f "${REPO}/cascade.py" >/dev/null 2>&1 || break
    sleep 1
  done
  # force-kill anything still alive
  pids="$(pgrep -f "${REPO}/cascade.py" 2>/dev/null || true)"
  # shellcheck disable=SC2086
  [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
else
  log "no running cascade found — starting a fresh one."
fi

log "launching cascade in the background (logging to ${REPO}/router.log)…"
nohup "$PYTHON" "$REPO/cascade.py" >> "$REPO/router.log" 2>&1 &
disown 2>/dev/null || true

report_health
