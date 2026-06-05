#!/usr/bin/env bash
#
# cascade — remote installer
#
# Clones the repo and runs install.sh in one step.
#
# Usage (one-liner):
#   curl -fsSL https://raw.githubusercontent.com/chrisluersen/cascade/main/get.sh | bash
#
# By default installs to ~/.local/share/cascade
# Override with: CASCADE_DIR=~/mydir bash <(curl ...)
#
set -uo pipefail

REPO_URL="https://github.com/chrisluersen/cascade.git"
INSTALL_DIR="${CASCADE_DIR:-$HOME/.local/share/cascade}"

log() { printf '\033[1;36m[cascade]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[cascade]\033[0m %s\n' "$*" >&2; }
ok()  { printf '\033[1;32m[cascade]\033[0m %s\n' "$*"; }

echo ""
echo "  ┌──────────────────────────────────┐"
echo "  │   cascade  ·  installer          │"
echo "  └──────────────────────────────────┘"
echo ""

# ── git ──────────────────────────────────────────────────────────────────────
if ! command -v git >/dev/null 2>&1; then
  err "git is required but not found."
  err "  Ubuntu/Debian:  sudo apt install git"
  err "  macOS:          brew install git"
  exit 1
fi

# ── Clone or update ───────────────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
  log "Found existing install at $INSTALL_DIR — updating..."
  git -C "$INSTALL_DIR" pull --ff-only --quiet
  ok "Updated to latest version"
else
  log "Installing to $INSTALL_DIR ..."
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --quiet "$REPO_URL" "$INSTALL_DIR"
  ok "Downloaded"
fi

# ── Run install.sh ────────────────────────────────────────────────────────────
bash "$INSTALL_DIR/install.sh"