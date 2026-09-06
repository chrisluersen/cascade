#!/usr/bin/env bash
# cascade-up.sh — launch Cascade with fresh probe state using system python (no venv).
# Usage: ./scripts/cascade-up.sh   (launches in background, logs to projects/cascade/cascade.log)
set -euo pipefail
cd "$(dirname "$0")/../projects/cascade"

# ---- 1) Kill any stale cascade on 8319 (by port, never taskkill python.exe) ----
PIDS=$(netstat -ano 2>/dev/null | grep ':8319' | grep LISTEN | awk '{print $5}' | sort -u || true)
if [ -n "$PIDS" ]; then
  echo "port 8319 in use by PID(s): $PIDS — killing"
  for p in $PIDS; do taskkill //F //PID "$p" >/dev/null 2>&1 || true; done
  sleep 1
fi

# ---- 2) Clear stale probe state so routing reflects reality at boot ----
rm -f cascade_state.json
rm -f "$HOME/.local/share/cascade/cascade_state.json" 2>/dev/null || true

# ---- 3) Launch.  Key gotcha: CASCADE_API_KEY in shell overrides .env/BW default — unset it.
unset CASCADE_API_KEY
mkdir -p "$HOME/.local/share/cascade"
export CASCADE_AUTH_FILE="$HOME/.local/share/cascade/auth.json"
export CASCADE_STATE_FILE="$HOME/.local/share/cascade/cascade_state.json"
nohup python3 cascade.py >> "$HOME/.local/share/cascade/cascade.log" 2>&1 &
echo "launched pid $! — log: $HOME/.local/share/cascade/cascade.log"
