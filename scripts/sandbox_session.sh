#!/usr/bin/env bash
# Run ONE desk session against the disposable paper account, from this checkout.
#
# This is the single documented command for the plan in
# docs/MARKET_HOURS_BACKLOG.md ("PLAN — pointing a FULL daily session at the
# disposable paper account"). It does three things and nothing else:
#
#   1. Strips the owner-facing messaging credentials out of the environment
#      and sets the mute the notifier actually honours, so the session is
#      incapable of messaging the owner rather than merely quiet.
#   2. Runs src/sandbox_preflight.py, which REFUSES if the session is standing
#      in the production checkout, if its data directory escapes this
#      checkout, if a database it did not create is already there, or if the
#      Alpaca key is not the pinned disposable one.
#   3. Only then starts main.py.
#
# Usage, from the root of a SEPARATE checkout with its own empty data/:
#
#   cp config/sandbox.env.example .env.sandbox     # fill in from your shell
#   scripts/sandbox_session.sh morning
#
# The session argument is passed straight through to main.py.

set -euo pipefail

CHECKOUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$CHECKOUT"

ENV_FILE="${QAMC_SANDBOX_ENV_FILE:-$CHECKOUT/.env.sandbox}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "SANDBOX REFUSED: no sandbox env file at $ENV_FILE." >&2
  echo "Copy config/sandbox.env.example to .env.sandbox and fill it in." >&2
  exit 1
fi

# Drop anything inherited from the operator's shell or the desk's unit BEFORE
# sourcing the sandbox file. Unsetting is what makes the token un-inheritable;
# the mute below is the second line, not the first.
unset TELEGRAM_BOT_TOKEN
unset TELEGRAM_CHAT_ID
unset ALPACA_API_KEY
unset ALPACA_SECRET_KEY
unset CREDENTIALS_DIRECTORY

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Re-assert after sourcing: a sandbox env file that sets a token is a mistake
# the preflight will refuse on, and the mute is forced regardless of the file.
unset TELEGRAM_BOT_TOKEN
unset TELEGRAM_CHAT_ID
export TELEGRAM_DISABLED=1

PYTHON="${QAMC_SANDBOX_PYTHON:-$CHECKOUT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi

"$PYTHON" -m src.sandbox_preflight

mkdir -p "$CHECKOUT/data"
MARKER="$CHECKOUT/data/SANDBOX_ACCOUNT"
if [[ ! -f "$MARKER" ]]; then
  echo "This data directory belongs to a disposable sandbox session." > "$MARKER"
  echo "Written by scripts/sandbox_session.sh. Safe to delete with the account." >> "$MARKER"
fi

exec "$PYTHON" main.py "$@"
