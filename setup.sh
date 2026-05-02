#!/usr/bin/env bash
# setup.sh – One-time setup helper (CI / fresh clone)
# For day-to-day development use start.sh instead — it is fully self-healing.
# Usage: bash setup.sh

# Don't use set -e so pip output doesn't abort the script
PYTHON=""
for candidate in python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
  if command -v "$candidate" &>/dev/null; then
    PYTHON="$candidate"; break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "ERROR: Python 3.12 not found. Run: brew install python@3.12" >&2
  exit 1
fi

echo "── Creating virtual environment ──"
"$PYTHON" -m venv .venv
source .venv/bin/activate

echo "── Installing dependencies ──"
pip install --upgrade pip
pip install -r requirements.txt && touch .venv/.install_stamp

echo "── Copying .env ──"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "  → Created .env from .env.example  (edit SECRET_KEY before production!)"
fi

echo "── Initialising database ──"
.venv/bin/python - <<'EOF'
from db import init_db
init_db()
print("  → Database initialised at data/solix.db")
EOF

echo ""
echo "All done! Start the development server with:"
echo "  bash start.sh"
echo ""
echo "Then open http://localhost:8080 and set your admin password on first visit."
