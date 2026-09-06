#!/usr/bin/env bash
# cascade-up.sh — launch Cascade from this repo with a fresh probe state.
#
# Repo-relative + portable: resolves its own location, so it works from any
# checkout (no machine-specific paths). State/logs live in the repo dir.
# Keys (CASCADE_API_KEY + provider keys) load from Bitwarden at startup.
#
# Usage:
#   ./scripts/cascade-up.sh
#
# Optional env overrides:
#   PORT=8319        # port to bind / health-check
#   PYTHON=python3   # interpreter (in the Hermes shell use python3, not python)
#
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || { echo "cannot cd to repo dir: $REPO"; exit 1; }
PORT="${PORT:-8319}"
PY="${PYTHON:-python3}"
LOG="$REPO/router.log"
ERR="$REPO/router-err.log"

log() { printf '\033[1;36m[cascade-up]\033[0m %s\n' "$*"; }

# 1) Already listening?  Nothing to do.
if netstat -ano 2>/dev/null | grep ":$PORT " | grep -q LISTEN; then
  log "cascade already RUNNING on :$PORT"
  netstat -ano 2>/dev/null | grep ":$PORT " | grep LISTEN
  exit 0
fi

# 2) Kill any stray cascade listening on the port (by PID — NEVER taskkill
#    /IM python, which would kill Hermes and every other python process).
STRAY=$(netstat -ano 2>/dev/null | grep ":$PORT " | grep LISTEN | awk '{print $NF}' | head -1)
if [ -n "${STRAY:-}" ]; then
  log "killing stray PID $STRAY on :$PORT"
  taskkill //F //PID "$STRAY" >/dev/null 2>&1 || true
  sleep 1
fi

# 3) Clear stale probe state so routing reflects reality at boot.
rm -f "$REPO/cascade_state.json"

# 4) Launch detached (no job that dies with the shell), append logs.
log "starting cascade from $REPO using $PY ..."
# Invoke cascade.py RELATIVE after cd — passing an MSYS /c/... path to native
# python.exe yields C:\c\... (no MSYS translation for native tools).
cd "$REPO"
nohup "$PY" cascade.py >>"$LOG" 2>>"$ERR" </dev/null &
disown 2>/dev/null || true
log "launched (logs: $LOG / $ERR)"

# 5) Wait for readiness. The port opens fast; full probe state lags a couple
#    minutes but never blocks serving.
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    log "cascade UP on :$PORT after ~${i}s"
    curl -s "http://127.0.0.1:$PORT/health" \
      | python3 -c "import sys,json; d=json.load(sys.stdin); print('status:', d.get('status'))" 2>/dev/null || true
    exit 0
  fi
  sleep 1
done
log "WARN: :$PORT not answering after 30s — check $ERR"
exit 1
