#!/usr/bin/env bash
# start.sh -- Bulletproof local dev launcher for Solix Performance Monitor
#
# What it does:
#   1. Ensures Python 3.12 is available
#   2. Creates / repairs the venv if missing or broken
#   3. Installs / upgrades deps (only when requirements.txt is newer)
#   4. Copies .env.example -> .env if .env is absent
#   5. Initialises the SQLite database (idempotent)
#   6. Finds a free port (default 8080, falls back to next available)
#   7. Kills any stale process already holding that port
#   8. Launches Flask and prints the URL
#
# Usage:
#   bash start.sh            # default port 8080
#   PORT=9000 bash start.sh  # custom port

set -euo pipefail

# ---------- helpers ----------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}--${RESET} $*"; }
success() { echo -e "${GREEN}OK${RESET} $*"; }
warn()    { echo -e "${YELLOW}!!${RESET} $*"; }
die()     { echo -e "${RED}ERROR${RESET} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------- 1. Python 3.12 ---------------------------------------------------
PYTHON=""
for candidate in python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3 python; do
  if command -v "$candidate" &>/dev/null; then
    ver=$("$candidate" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null || true)
    if [[ "$ver" == "(3, 12)" ]] || [[ "$ver" > "(3, 11)" ]]; then
      PYTHON="$candidate"
      break
    fi
  fi
done
[[ -z "$PYTHON" ]] && die "Python 3.12+ not found. Install with: brew install python@3.12"
success "Python: $("$PYTHON" --version)"

# ---------- 2. Virtual environment -------------------------------------------
VENV_PYTHON=".venv/bin/python"
VENV_PIP=".venv/bin/pip"

rebuild_venv() {
  info "Creating virtual environment..."
  rm -rf .venv
  "$PYTHON" -m venv .venv
}

if [[ ! -f "$VENV_PYTHON" ]]; then
  rebuild_venv
elif ! "$VENV_PYTHON" -c "import sys; assert sys.version_info >= (3,12)" &>/dev/null; then
  warn "Venv Python is too old -- rebuilding..."
  rebuild_venv
else
  success "Virtual environment OK"
fi

# ---------- 3. Dependencies --------------------------------------------------
STAMP_FILE=".venv/.install_stamp"
NEEDS_INSTALL=0
[[ ! -f "$STAMP_FILE" ]] && NEEDS_INSTALL=1
[[ requirements.txt -nt "$STAMP_FILE" ]] && NEEDS_INSTALL=1 && info "requirements.txt changed -- upgrading packages..."

if [[ $NEEDS_INSTALL -eq 1 ]]; then
  info "Installing dependencies (may take a minute on first run)..."
  "$VENV_PIP" install --upgrade pip --quiet
  if "$VENV_PIP" install -r requirements.txt --quiet; then
    touch "$STAMP_FILE"
    success "Dependencies installed"
  else
    warn "pip install failed -- retrying with --no-cache-dir..."
    "$VENV_PIP" install -r requirements.txt --no-cache-dir --quiet \
      && touch "$STAMP_FILE" && success "Dependencies installed" \
      || die "Dependency installation failed. Check requirements.txt."
  fi
else
  success "Dependencies up-to-date"
fi

# ---------- 4. .env file -----------------------------------------------------
if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    warn ".env created from .env.example -- edit SECRET_KEY before deploying!"
  else
    warn "No .env or .env.example found -- relying on environment variables."
  fi
else
  success ".env present"
fi

# ---------- 5. Database ------------------------------------------------------
info "Initialising database (idempotent)..."
"$VENV_PYTHON" - <<'EOF'
from db import init_db
init_db()
print("   data/solix.db ready")
EOF

# ---------- 6. Pick a free port ----------------------------------------------
DESIRED_PORT="${PORT:-8080}"

is_port_free() {
  ! lsof -iTCP:"$1" -sTCP:LISTEN -t &>/dev/null
}

PORT_TO_USE="$DESIRED_PORT"
if ! is_port_free "$PORT_TO_USE"; then
  PIDS=$(lsof -iTCP:"$PORT_TO_USE" -sTCP:LISTEN -t 2>/dev/null || true)
  if [[ -n "$PIDS" ]]; then
    MINE=""
    for pid in $PIDS; do
      owner=$(ps -o user= -p "$pid" 2>/dev/null | tr -d ' ')
      if [[ "$owner" == "$(whoami)" ]]; then
        MINE="$MINE $pid"
      fi
    done
    if [[ -n "$MINE" ]]; then
      warn "Port ${PORT_TO_USE} occupied by own process(es):${MINE} -- killing..."
      kill $MINE 2>/dev/null || true
      sleep 1
    fi
  fi

  if ! is_port_free "$PORT_TO_USE"; then
    warn "Port ${PORT_TO_USE} still in use (system process?) -- scanning for next free port..."
    for try_port in $(seq $((DESIRED_PORT+1)) $((DESIRED_PORT+20))); do
      if is_port_free "$try_port"; then
        PORT_TO_USE="$try_port"
        break
      fi
    done
    if [[ "$PORT_TO_USE" -eq "$DESIRED_PORT" ]]; then
      die "No free port found in range ${DESIRED_PORT}-$((DESIRED_PORT+20))."
    fi
    warn "Using fallback port ${PORT_TO_USE}"
  fi
fi

# ---------- 7. Launch Flask --------------------------------------------------
echo ""
echo -e "${BOLD}${GREEN}Starting Solix Monitor on port ${PORT_TO_USE}${RESET}"
echo -e "  Open: ${CYAN}http://localhost:${PORT_TO_USE}${RESET}"
echo -e "  Stop: Ctrl-C"
echo ""

export PORT="$PORT_TO_USE"
exec "$VENV_PYTHON" app.py
