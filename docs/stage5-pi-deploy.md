# Stage 5a — Pi deploy (email half)

Scope: get the Pi sending the digest every Monday on its own. The Mac-side
launchd glue (Reminders → TSV → rsync to Pi, and Pi `outbox/` → iCloud vault)
is **stage 5b and is not covered here** — see "Known gaps" at the end for what
that means in practice.

Why this document exists: as of 2026-08-26 the Pi had nothing on it. Stages
1–4 were complete and a real digest was sent by hand from the Mac on
2026-08-21, but nothing was ever deployed, so no Monday email could have
arrived. There was no bug to find.

---

## 0. Before you start

You need, on the Pi: a shell, sudo, and outbound network. On the Mac: the
current `data/sources.tsv`, and your two iCloud SMTP values.

Substitute your own values throughout:

| Placeholder | Meaning |
|---|---|
| `PI` | the Pi's hostname or IP, e.g. `raspberrypi.local` |
| `PIUSER` | the Pi login, e.g. `pi` |

---

## 1. Prepare the Pi

```bash
ssh PIUSER@PI

sudo apt update && sudo apt install -y git

# uv is not in apt. This installs to ~/.local/bin.
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
uv --version
```

**Set the timezone.** A stock Raspberry Pi OS image runs UTC. The timer says
`OnCalendar=Mon *-*-* 06:00:00`, which systemd resolves against the *system*
timezone — on a UTC Pi that fires at 02:00 ET, not 06:00.

```bash
sudo timedatectl set-timezone America/New_York
timedatectl        # confirm "Time zone: America/New_York"
```

---

## 2. Get the code

```bash
git clone https://github.com/lenz-m/ai-digest.git ~/ai-digest
cd ~/ai-digest
uv sync
uv run pytest      # 243 offline tests, no API calls, no cost
```

A green pytest run here proves the install before you spend a cent.

---

## 3. Secrets — `.env`

The Pi has no Keychain equivalent, so `.env` is the deliberate choice here
(see CLAUDE.md § Secrets). `pipeline/config.py` calls `load_dotenv()`
unconditionally, and the systemd unit sets `WorkingDirectory` so dotenv finds
it.

```bash
cd ~/ai-digest
cp .env.example .env
chmod 600 .env
nano .env
```

Fill in `ANTHROPIC_API_KEY`, `SMTP_USERNAME`, `SMTP_APP_PASSWORD`,
`AI_DIGEST_TO`, `AI_DIGEST_FROM`.

Two things that will otherwise cost you an evening:

- **`AI_DIGEST_FROM` must be an address the iCloud account actually owns.**
  An unrecognised From gets a hard 550, which `send.py` correctly refuses to
  retry. Leaving it equal to `SMTP_USERNAME` (your full Apple ID) is
  definitionally safe.
- **Make `AI_DIGEST_TO` a different address from `AI_DIGEST_FROM` if you
  possibly can.** Verified 2026-08-21: a self-send is accepted and delivered
  but **does not reliably appear in Mail.app** — only at icloud.com. On the Pi
  nobody watches a console, so "no email arrived" is the only symptom you get,
  and you will spend the evening debugging SMTP that worked perfectly.

### Backup exposure — read before you paste real values

This Pi runs two nightly backup jobs, set up independently of ai-digest:

- **02:00 `pi-snapshot.sh`** — rsyncs `/home`, `/etc`, `/srv`, `/opt` to a USB
  flash drive, keeping 30 daily dated snapshots via hardlinks.
- **02:30 `rpi-clone -l mmcblk0`** — writes a full bootable clone of the
  system to the SD card's second partition.

`~/ai-digest/.env` is under `/home`. **Both jobs copy it.** From the first
night onward, the live values of `ANTHROPIC_API_KEY`, `SMTP_USERNAME` and
`SMTP_APP_PASSWORD` exist on an unencrypted ext4 flash drive and on a bootable
SD-card clone, in addition to the Pi's root filesystem.

`chmod 600` does not prevent any of this. It stops other unprivileged users on
the Pi — of which there are none — and is simply not a factor for a root-run
rsync. Treat the `600` above as hygiene, not as a control.

Two properties matter more than the number of copies:

- **Retention outlives deletion.** With a 30-snapshot rotation, deleting or
  rewriting `.env` does not remove the old value from the flash drive for up
  to 30 days. Rotating a key at the vendor's console takes effect the moment
  you click; scrubbing it from backup media does not.
- **Physical possession of the flash drive or the SD card is possession of the
  credentials.** No login is involved — the ext4 filesystem mounts anywhere.

**This is a different risk class from what was previously on this Pi, not more
of the same.** Before ai-digest the only credential here was a repo-scoped
GitHub deploy key, which is worthless to anyone without the machine it is
bound to. An Anthropic API key and an iCloud app-specific password are *bearer
strings*: they work from any IP, for anyone holding them, with no reference to
this hardware. Backup media whose compromise was low-consequence for a deploy
key is not low-consequence for these.

The two steps that follow do not stop the copying. They bound what a copy is
worth, and make replacement fast.

### Bound the blast radius: set a spend limit

**This is the primary containment measure.** File permissions cannot protect a
string that has already left the machine; a hard cap can, because it applies
to whoever is using the key, wherever they are.

Before the first real run, in the **Anthropic Console**, set a monthly spend
limit on the workspace this key belongs to.

Size it from measured cost: a full run is **~$0.64**, so weekly operation is
about **$2.80/month**. A **$10–15/month** limit absorbs a re-run or two and
still converts a leaked key from an unbounded liability into a bounded one.

Note what does *not* do this job: `AI_DIGEST_COST_CEILING_USD` (default $5)
is a per-run pre-flight check inside this pipeline. It protects you against a
runaway loop in your own code on this Pi. It is config on the same disk as the
key, and it has no bearing at all on someone using that key from somewhere
else. Only the console limit applies to a thief.

Worth doing while you are there: issue a **separate key for the Pi** rather
than reusing the Mac's. Revoking a shared key breaks your dev loop at the
moment you least want it broken.

### The seen-set

`AI_DIGEST_COMMIT_SEEN` is commented out in the template, which means off.
Off, the pipeline never records which articles it has already shown you, so
**every Monday re-ingests everything and you get largely the same digest**.

The gate that kept it off has been cleared: it was waiting on score-stage
failures dropping near zero (now 1 of 60, 1.7%) and on the `max_survivors`
round-robin fix being verified against a real run (commit `a126671`; the
Aug 21 log shows WSJ and Economist items reaching the score stage for the
first time). Uncomment it:

```
AI_DIGEST_COMMIT_SEEN=true
```

If you dislike the result, `rm ~/ai-digest/cache/seen.json` returns you to a
blank slate. It is a file, not a one-way door.

---

## 4. Seed `data/sources.tsv`

**This is the step a naive clone-and-run dies on.** `.gitignore` has `data/*`
with only `manual_sources.tsv` re-included, so your 44 real sources are not in
the repo. The cache fallback `cache/sources_last_good.tsv` is gitignored too.
With neither present, `ingest.py` raises `IngestError` at stage 1, before any
API call.

From the **Mac**:

```bash
scp ~/projects/ai-digest/data/sources.tsv PIUSER@PI:~/ai-digest/data/sources.tsv
```

Until stage 5b builds the Reminders rsync, this file is a **static snapshot**.
Sources you add in Reminders will not reach the Pi. Re-run the scp when you
change them.

---

## 5. Install and arm

```bash
cd ~/ai-digest
./deploy/bootstrap-pi.sh
```

The script re-checks everything above, renders `deploy/ai-digest.service.template`
with the real user/paths/uv location, installs both units, and arms the timer —
but it **refuses to arm** if `.env` still holds placeholders or `sources.tsv`
is missing, printing what to fix.

---

## 6. Pre-flight: confirm the secret has not escaped

Run this **before** the smoke test. It takes seconds and it is the difference
between a contained secret and a published one.

```bash
cd ~/ai-digest
git check-ignore -v .env           # must print the matching .gitignore rule
git log --all --oneline -- .env    # must print NOTHING
ls -la .env                        # must be -rw------- and owned by you
```

How to read each:

- **`git check-ignore -v .env`** should name the rule that ignores it
  (`.gitignore:1:.env`). Silence, or exit status 1, means `.env` is *not*
  ignored here and a single `git add -A` would commit it.
- **`git log --all --oneline -- .env` must be empty.** `--all` matters: it
  covers every branch and tag, not just the one you are on. If it prints even
  one commit, **the key is in the repository's history and has been pushed to
  GitHub. Rotate `ANTHROPIC_API_KEY` and the iCloud app-specific password
  before doing anything else — before the smoke test, before arming the
  timer.** Assume the value is compromised regardless of whether the remote is
  public: history is fetched by every clone and mirrored by tooling you do not
  control. Rewriting history is the *second* step and does not substitute for
  rotation, because the old value has already been distributed.
- **`ls -la .env`** must show `-rw-------`. This does not defend against the
  backups described in § 3; it defends against the ordinary mistakes.

---

## 7. Smoke test

Cheap first, real last.

```bash
# ~$0.19. Exercises fetch + Anthropic + secrets loading. Sends nothing.
cd ~/ai-digest && uv run python -m pipeline.run --limit-sources 3
```

Then the one that matters:

```bash
sudo systemctl start ai-digest.service
journalctl -u ai-digest -f
```

**Run this final test through systemd, not by hand.** Every failure this unit
is written to prevent — `uv` not on PATH, the 90-second default timeout
killing a 2-hour batch poll, a C locale mangling the emoji in the vault note
filename — appears *only* under systemd. A hand-run `uv run` proves nothing
about them.

This sends a real email and costs ~$0.64 (batch should be roughly half).

---

## 8. Verify

```bash
systemctl list-timers ai-digest.timer    # NEXT should be the coming Monday 06:00
systemctl status ai-digest.service       # exit status of the last run
ls -lat ~/ai-digest/logs/ | head
ls -la  ~/ai-digest/outbox/Digests/      # the vault note, awaiting stage 5b
```

Exit codes: `0` ok · `1` send failed · `2` bad flag combination ·
`3` fatal API error (billing or auth).

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `status=203/EXEC` | `uv` path wrong in the unit | `which uv` on the Pi, re-run bootstrap |
| Killed ~90s in | `TimeoutStartSec` missing | confirm it is in the installed unit, not just the template |
| `IngestError` immediately | `data/sources.tsv` absent | § 4 |
| exit 3 | Anthropic credit or auth | check the balance — this exact failure ate the Aug 20 run |
| exit 1, `535` in journal | bad SMTP credentials | app-specific password, not the Apple ID password |
| exit 1, `550` in journal | `AI_DIGEST_FROM` not owned | § 3 |
| exit 1, repeated `4xx` | Pi's outbound IP greylisted | the documented iCloud risk; if persistent, fall back to a transactional API rather than re-architecting |
| exit 0 but no email | self-send not surfacing | check icloud.com **and its Junk folder**, not Mail.app |
| Timer never fires | not enabled, or UTC | `systemctl is-enabled ai-digest.timer`; `timedatectl` |
| Same digest twice | `commit_seen` off | § 3 |

---

## Credential recovery

What a from-scratch rebuild needs. **Names and sources only — no values
belong in this file, or in any file in this repository.**

The blunt fact first: **none of the three secrets below can be recovered.**
The Anthropic console shows a key once at creation; Apple shows an
app-specific password once at generation. A rebuild means *issuing new ones*,
not retrieving the old ones. Plan for replacement, not recovery.

| Variable | What it is | Where it comes from | How to rotate |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key — a bearer credential | Anthropic Console → API keys | Create a new key, paste into `.env`, delete the old key. Effective immediately. Re-apply the spend limit (§ 3) to the new key's workspace. |
| `SMTP_USERNAME` | The full Apple ID used for iCloud SMTP auth | Your Apple ID | Not a secret on its own, but it identifies the account. Changes only if you change Apple ID. |
| `SMTP_APP_PASSWORD` | iCloud **app-specific** password — never the Apple ID password | appleid.apple.com → Sign-In and Security → App-Specific Passwords | Revoke the old entry there, generate a replacement, paste into `.env`. **Revocable independently of the Apple ID password:** revoking it does not change your Apple ID password, does not sign you out of any device, and affects nothing but this one integration. That independence is what makes it the safe credential to hand a Pi. |
| `AI_DIGEST_TO` | Recipient address | Your choice | Not a credential. |
| `AI_DIGEST_FROM` | Sender; must be an address the iCloud account actually owns, or iCloud returns a hard 550 | Your Apple ID, or an alias it owns | Not a credential. |

**The same three secrets live in two places**, and a rotation must update
both: `.env` on the Pi, and the **macOS Keychain** on the Mac under
`ai-digest-anthropic-api-key`, `ai-digest-smtp-username`,
`ai-digest-smtp-app-password` (see `scripts/run_with_secrets.sh`). There is no
`.env` on the Mac and there should never be one.

**If you rotate because of a suspected leak**, rotate at the vendor first and
update `.env` second. Deleting the old value from the Pi does nothing while
the old value still authenticates — and per § 3 it survives in the flash-drive
snapshots for up to 30 days regardless.

---

## Known gaps after this deploy

1. **No Mac glue (stage 5b).** `data/sources.tsv` is a static snapshot, and
   `outbox/Digests/` accumulates vault notes that nothing moves into iCloud.
   The Pi has no route to iCloud Drive — that split is the whole reason for
   this architecture and is not up for relitigation.
2. **No failure alerting.** A failed run is silent; a missing email is the
   only symptom. `OnFailure=` with a notifier is the natural next addition.
3. **The Unicode round trip is still untested.** The note filename starts with
   an emoji and rsyncing non-ASCII names Pi (Linux, NFC) → Mac (APFS) is a
   known duplicate-file source. Verify before trusting the archive half.
4. **`.env` is copied into the nightly backups and nothing here changes
   that** (§ 3). The accepted position is: the spend limit bounds the cost of
   an Anthropic key leak, the app-specific password is revocable in isolation,
   and both are replaceable in minutes. If that ever stops feeling
   proportionate, the fix is to move the Pi's secrets out of `/home` and
   exclude that path from `pi-snapshot.sh` — a change to the backup job, not
   to this pipeline.
5. **The filter still passes ~71%** (312 of 441), so `max_survivors` remains
   the real curator. With `commit_seen` now on, capped items are permanently
   dropped rather than deferred. Unchanged by this deploy, but it matters more
   than it did.
