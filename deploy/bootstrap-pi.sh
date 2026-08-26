#!/usr/bin/env bash
#
# Bootstrap (or update) the ai-digest deployment on the Raspberry Pi.
#
# Idempotent: safe to re-run after a `git push` to pull new code and
# reinstall the units. It never overwrites .env and never touches
# cache/seen.json.
#
# Usage, from a checkout on the Pi:
#     ./deploy/bootstrap-pi.sh
#
# Deliberately does NOT do two things, because neither can be automated
# safely from here:
#   * put real secrets into .env  -- secret material, and this script's
#     output goes to a terminal and possibly a scrollback buffer.
#   * seed data/sources.tsv       -- it is gitignored and lives on the Mac.
# The runbook (docs/stage5-pi-deploy.md) covers both. This script checks
# for them and refuses to arm the timer until they are real.

set -euo pipefail

REPO_URL="${AI_DIGEST_REPO_URL:-https://github.com/lenz-m/ai-digest.git}"
APP_DIR="${AI_DIGEST_HOME:-$HOME/ai-digest}"
UNIT_DIR=/etc/systemd/system
RUN_USER="$(id -un)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31merror:\033[0m %s\n\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- prereqs
say "Checking prerequisites"

command -v git >/dev/null 2>&1 || die "git missing. Run: sudo apt update && sudo apt install -y git"
ok "git $(git --version | awk '{print $3}')"

# uv installs to ~/.local/bin, which is on an interactive PATH but not on
# systemd's. Resolve it to an absolute path now and bake that into the unit.
UV=""
for cand in "$(command -v uv 2>/dev/null || true)" "$HOME/.local/bin/uv" /usr/local/bin/uv; do
    [ -n "$cand" ] && [ -x "$cand" ] && { UV="$cand"; break; }
done
[ -n "$UV" ] || die "uv missing. Run: curl -LsSf https://astral.sh/uv/install.sh | sh
       then re-open the shell (or: source \$HOME/.local/bin/env) and try again."
ok "uv at $UV"

# ------------------------------------------------------------------- code
say "Syncing code into $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
    ok "pulled latest"
else
    [ -e "$APP_DIR" ] && die "$APP_DIR exists but is not a git checkout. Move it aside first."
    git clone "$REPO_URL" "$APP_DIR"
    ok "cloned"
fi

say "Installing dependencies"
( cd "$APP_DIR" && "$UV" sync )
ok "uv sync complete"

mkdir -p "$APP_DIR/logs" "$APP_DIR/outbox/Digests" "$APP_DIR/preview" "$APP_DIR/cache"
ok "runtime directories present"

# --------------------------------------------------------------- secrets
say "Checking secrets"
ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cp "$APP_DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    warn "created $ENV_FILE from the template -- it is all PLACEHOLDERS."
    NEEDS_ENV=1
else
    chmod 600 "$ENV_FILE"
    ok ".env present (mode 600)"
    NEEDS_ENV=0
    # Placeholder values would fail at point of use with a confusing error
    # hours into the run, so catch them here instead.
    if grep -qE '^(ANTHROPIC_API_KEY=sk-ant-\.\.\.|SMTP_APP_PASSWORD=xxxx|SMTP_USERNAME=you@|AI_DIGEST_TO=you@|AI_DIGEST_FROM=you@)' "$ENV_FILE"; then
        warn ".env still contains template placeholders."
        NEEDS_ENV=1
    fi
fi

# --------------------------------------------------------------- sources
say "Checking source list"
SOURCES="$APP_DIR/data/sources.tsv"
NEEDS_SOURCES=0
if [ -s "$SOURCES" ]; then
    ok "data/sources.tsv present ($(($(wc -l < "$SOURCES") - 1)) rows)"
else
    warn "data/sources.tsv is MISSING. It is gitignored (data/* in .gitignore),"
    warn "so a fresh clone never has it, and ingest.py raises IngestError"
    warn "before a single API call. Copy it from the Mac:"
    warn "    scp ~/projects/ai-digest/data/sources.tsv ${RUN_USER}@\$(hostname):${SOURCES}"
    NEEDS_SOURCES=1
fi

# ----------------------------------------------------------------- units
say "Installing systemd units"
TMP_UNIT="$(mktemp)"
trap 'rm -f "$TMP_UNIT"' EXIT
sed -e "s|__USER__|${RUN_USER}|g" \
    -e "s|__APP_DIR__|${APP_DIR}|g" \
    -e "s|__UV__|${UV}|g" \
    "$APP_DIR/deploy/ai-digest.service.template" > "$TMP_UNIT"

sudo install -m 644 "$TMP_UNIT" "$UNIT_DIR/ai-digest.service"
sudo install -m 644 "$APP_DIR/deploy/ai-digest.timer" "$UNIT_DIR/ai-digest.timer"
sudo systemctl daemon-reload
ok "units installed to $UNIT_DIR"

# -------------------------------------------------------------- timezone
TZ_NOW="$(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"
if [ "$TZ_NOW" = "UTC" ] || [ "$TZ_NOW" = "Etc/UTC" ]; then
    warn "Pi timezone is $TZ_NOW. OnCalendar=Mon 06:00 would fire at 02:00 ET."
    warn "Fix with: sudo timedatectl set-timezone America/New_York"
else
    ok "timezone is $TZ_NOW"
fi

# ----------------------------------------------------------------- arm it
if [ "$NEEDS_ENV" -eq 1 ] || [ "$NEEDS_SOURCES" -eq 1 ]; then
    say "NOT arming the timer yet"
    echo "    Fill in the gaps flagged above, then run:"
    echo "        sudo systemctl enable --now ai-digest.timer"
    exit 0
fi

sudo systemctl enable --now ai-digest.timer
say "Done -- timer armed"
systemctl list-timers ai-digest.timer --no-pager || true
cat <<'NEXT'

    Verify without waiting for Monday (this sends a REAL email):
        sudo systemctl start ai-digest.service
        journalctl -u ai-digest -f

    Check the last run:
        systemctl status ai-digest.service
        ls -lat ~/ai-digest/logs/ | head

    Exit codes: 0 ok | 1 send failed | 2 bad flags | 3 fatal API (billing/auth)
NEXT
