#!/usr/bin/env bash
# Launch Loopi on http://localhost:3300
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Setting up (first run only)…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
  ./.venv/bin/python -m playwright install chromium
fi
# Optional secrets: CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) and/or ANTHROPIC_API_KEY
if [ -f .env ]; then set -a; . ./.env; set +a; fi
if [ -z "$ANTHROPIC_API_KEY" ] && [ -f ~/.zshrc ]; then
  eval "$(grep '^export ANTHROPIC_API_KEY=' ~/.zshrc || true)"
fi
# Claude Code exports a proxy base URL into child shells; the real API must be used here.
unset ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN
exec ./.venv/bin/python server.py < /dev/null
