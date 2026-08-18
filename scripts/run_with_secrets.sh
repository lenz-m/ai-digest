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
    security find-generic-password -a "$USER" -s "$service" -w 2>/dev/null
}

ANTHROPIC_API_KEY="$(fetch_secret ai-digest-anthropic-api-key)"
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "No Anthropic API key found in Keychain (service: ai-digest-anthropic-api-key)." >&2
    echo "Add one via Keychain Access.app, or (note: plain 'read -p' breaks under zsh -- use printf instead):" >&2
    echo '  printf "Paste your Anthropic API key: " && read -s KEY && echo && security add-generic-password -U -a "$USER" -s ai-digest-anthropic-api-key -w "$KEY" && unset KEY' >&2
    exit 1
fi
export ANTHROPIC_API_KEY

exec "$@"
