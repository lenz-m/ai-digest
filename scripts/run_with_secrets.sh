#!/usr/bin/env bash
# Mac-only convenience wrapper: pulls secrets out of macOS Keychain and
# injects them into the environment of ONE subprocess -- never written to
# disk, never left sitting in .env, never in shell history. This is the
# Mac half of the "Keychain on the Mac, .env on the Pi" split documented in
# CLAUDE.md -- the Pi has no Keychain equivalent, so it stays on .env, but
# the Mac does have one and should use it.
#
# Usage:
#   ./scripts/run_with_secrets.sh uv run python -m pipeline.run
#   ./scripts/run_with_secrets.sh uv run pytest
#
# Add a new secret: extend the block below, then add it to Keychain with
#   security add-generic-password -a "$USER" -s <service-name> -w
# (or via Keychain Access.app, which avoids shell history entirely).

set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "usage: $0 <command to run with secrets in its environment>" >&2
    exit 1
fi

fetch_secret() {
    local service="$1"
    # `|| true` is load-bearing under `set -e`: without it, a MISSING secret
    # makes the command substitution fail, which aborts the whole script at
    # the assignment -- so the caller's own "not found, here's how to add it"
    # message below could never be reached, and the SMTP block's deliberate
    # warn-and-continue would become an exit.
    security find-generic-password -a "$USER" -s "$service" -w 2>/dev/null || true
}

ANTHROPIC_API_KEY="$(fetch_secret ai-digest-anthropic-api-key)"
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "No Anthropic API key found in Keychain (service: ai-digest-anthropic-api-key)." >&2
    echo "Add one via Keychain Access.app, or (note: plain 'read -p' breaks under zsh -- use printf instead):" >&2
    echo '  printf "Paste your Anthropic API key: " && read -s KEY && echo && security add-generic-password -U -a "$USER" -s ai-digest-anthropic-api-key -w "$KEY" && unset KEY' >&2
    exit 1
fi
export ANTHROPIC_API_KEY

# SMTP (stage 4). Unlike the Anthropic key, missing creds WARN rather than
# exit: dry runs and `uv run pytest` need no SMTP at all, and failing the
# wrapper without them would break the everyday path. send.py raises a clear,
# self-solving error at point of use when --apply is set and these are absent.
SMTP_USERNAME="$(fetch_secret ai-digest-smtp-username)"
SMTP_APP_PASSWORD="$(fetch_secret ai-digest-smtp-app-password)"
if [ -n "$SMTP_USERNAME" ] && [ -n "$SMTP_APP_PASSWORD" ]; then
    export SMTP_USERNAME SMTP_APP_PASSWORD
else
    echo "note: SMTP creds not in Keychain (ai-digest-smtp-username / ai-digest-smtp-app-password)." >&2
    echo "      Dry runs and pytest are unaffected; --apply will fail with instructions." >&2
    echo "      See docs/stage4-send-plan.md Q4 for the one-time setup." >&2
fi

exec "$@"
