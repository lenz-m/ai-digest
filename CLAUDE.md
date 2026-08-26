# ai-digest

Weekly AI-news digest: fetch → dedupe → rank → summarize → email (primary) +
Obsidian vault archive (secondary). Full design rationale was worked out in
chat before any code — see the summary below for the decisions that matter
for future changes.

## Architecture (decided, don't relitigate)

- **Raspberry Pi** (always on) runs the actual pipeline: fetch → dedupe →
  rank → summarize → send email → write finished `.md` to local `outbox/`.
- **MacBook Air** (launchd, hourly + on wake) does two dumb errands: dump
  Reminders → TSV → rsync to Pi; rsync Pi's `outbox/` → iCloud Obsidian
  vault, then clear it. The Pi cannot write iCloud Drive directly (rclone's
  iclouddrive backend is disqualified — real Apple ID password, ADP-off
  requirement, 30-day trust token expiry) — that's why the latency-tolerant
  half (archiving) lives on the intermittent machine, and the punctual half
  (email) lives on the always-on one.
- Python via `uv`, no Docker, dry-run by default (`--apply` to actually
  send/write), JSON caches with stable keys alongside scripts, pytest over
  fetch/dedupe/rank logic — all matching the notion-to-obsidian conventions
  already in use for the vault.

## Ranking: two protected objectives

Every candidate gets two independent scores from one structured LLM call:
`org_score` (delivery-economics relevance, for the **"Delivery economics"**
section) and `fluency_score` (AI-practitioner fluency, for **"Practitioner
depth"**) — each with its own one-line reason, never a bare verdict. (Those
two sections were called "For the org" and "For you" until 2026-08-21;
older notes below still use the old names. The reason strings are still
produced and still drive the console digest — they are simply no longer
*rendered* into the email or vault note; see the presentation-pass note in
Stage 4a.)

Selection order matters: top 5 by `org_score` are picked first and removed
from the pool, *then* top 3 by `fluency_score` are picked from what's left.
That removal order is what makes the fluency section actually protected — if fluency
were ranked against the full pool including org picks, a slow AI-fluency
week could still get crowded out by overlapping stories.

## Signal vs. vendor noise

Source-level trust tier (`independent_analysis` / `independent_news` /
`vendor`), classified once per source and cached in
`cache/trust_tiers.json`, human-correctable (edited entries win over the
seed). Built in `pipeline/trust.py`, seeded for all 37 known sources. The
tier is injected into each score prompt so vendor-published content must
clear a HIGHER bar for `org_score` (and its adoption claims are treated
skeptically) — it does NOT penalize `fluency_score`, since a vendor
engineering post can be genuinely fluency-relevant on its technical merits.
Item-level marketing-language + unverified-adoption-claim flag baked into
the same scoring prompt — downweights, doesn't hard-exclude. Flag shows in
the reason string.

**`org_score` is a STRATEGY axis, not an implementation one** (sharpened
after the first full UAT run surfaced CTO-implementation content — "govern
Gemini with BigQuery", "accelerate model upgrades" — in the org slots). The
rubric now down-ranks HARD (score <20) any "how to deploy / govern /
evaluate / implement / build with an AI tool" content, product tutorials,
feature announcements, and model release notes — that's work a CTO
evaluates, not strategy, even when the tool could automate a billable task.
`org_score` is reserved for shifts in the *economics* of delivery work:
staffing/pyramid, pricing, buyer/procurement behavior, competitor/market
moves, margin/valuation signals for services firms, and credible
*independent* adoption data.

## API-call cost discipline (hard requirement)

A previous digest build called the LLM per-item for a cheap classification
step and ran ~3x over budget before it was noticed. To prevent a repeat:

- Dedupe happens *before* any LLM call — an already-seen item never reaches
  the API, let alone the expensive stage.
- The cheap filter pass batches many candidates into one structured-output
  call, never one call per item.
- Score + summary is a single combined call per surviving item — never a
  separate summarization round-trip re-sending the same article text.
- Batch API for both LLM passes (50% off; weekly cadence has zero latency
  pressure, no reason to pay for synchronous calls).
- Prompt caching on the repeated rubric/system-prompt text.
- Hard per-run token/cost ceiling — a run that's about to blow the budget
  stops and logs instead of silently spending.
- Every run (dry-run or `--apply`) prints and logs actual token counts +
  estimated cost, so drift is visible the next morning, not next quarter.
- `tests/` will include a regression test asserting "N candidate items → 1
  API call" for the batched filter stage specifically, so a future refactor
  can't silently reintroduce per-item calls without failing.

Rough cost estimate at current (July 2026) Anthropic pricing: ~$0.80–1.00 per
weekly run, ~$40–55/year. See chat history for the full breakdown.

## Vault archive format

New type: `type: ai-digest`, filename prefix `🗞️`, new top-level folder
`Digests/` (not Journal/ or Media/ — it's neither personal reflection nor a
single piece of media). `concepts:` frontmatter = union of that week's
item-level concept tags. Stretch goal: auto-promote the #1 "For you" item
into a `Media/🔗 <title>.md` stub matching the existing online-article
convention (`type: online-article`, `author: "[[👤 ]]"`, `date-added`, empty
`cover:` for `add_covers.py` to enrich later).

**Outbox mirrors the vault's folder structure** — the pipeline writes
`outbox/Digests/🗞️ AI Digest <date>.md`, not a flat `outbox/*.md`. Reason:
the Media-stub stretch goal above means outbox will eventually carry files
bound for **two different vault folders**, and a flat outbox forces stage
5's rsync to become a per-file router. Mirroring keeps rsync a plain
directory mirror, and adding `outbox/Media/` later then costs nothing.

Corollary: **`outbox/` holds only vault-bound files.** Dry-run previews go
to `preview/` (gitignored), never into the directory stage 5 sweeps —
otherwise every stale preview gets archived into the vault as if it were
real.

**Stage 5 gotcha to test for, not yet addressed:** the note filename starts
with 🗞️ and the vault convention is full of emoji filenames. Rsyncing
non-ASCII filenames Pi (Linux, NFC) → Mac (APFS) is a known source of
duplicate-file bugs from Unicode normalization mismatch. Verify a round trip
before trusting the archive half.

## Secrets

Split by machine, matching what each actually has available:

- **Mac (dev):** macOS Keychain. `scripts/run_with_secrets.sh` fetches
  secrets via `security find-generic-password` and injects them into the
  environment of a single subprocess (`./scripts/run_with_secrets.sh uv run
  python -m pipeline.run`) -- never written to disk, never in `.env`, never
  in shell history. Same pattern already used for the TMDB key in the vault
  scripts. One-time setup: add the key via Keychain Access.app, or
  `security add-generic-password -a "$USER" -s ai-digest-anthropic-api-key -w`
  (use `read -s` to pipe the value in rather than typing it inline, so it
  never lands in shell history).
- **Pi:** no real Keychain-equivalent exists on a headless single-user Pi
  (no TPM). Least-bad option, chosen deliberately over something like sops
  that would add complexity without adding real protection on this
  hardware: `.env` file, `chmod 600`, owned by the pi user, loaded via
  `python-dotenv` (NOT systemd `EnvironmentFile` — see Stage 5a for why
  pointing both parsers at one secret file is a trap).

**`chmod 600` is NOT the whole story on this Pi — it is barely a control at
all.** The Pi runs two nightly backup jobs, set up separately from ai-digest:
`pi-snapshot.sh` (02:00) rsyncs `/home`, `/etc`, `/srv`, `/opt` to an
unencrypted ext4 USB flash drive, keeping 30 rolling daily snapshots via
hardlinks; `rpi-clone -l mmcblk0` (02:30) writes a full bootable clone to the
SD card's second partition. `~/ai-digest/.env` sits under `/home`, so **both
jobs copy it**. Mode 600 stops other unprivileged users — of which there are
none on this box — and is simply not a factor for a root-run rsync.

**This is a different risk class from what the Pi held before, not more of the
same.** The only credential on it previously was a repo-scoped GitHub deploy
key, worthless to anyone without the machine it is bound to.
`ANTHROPIC_API_KEY` and `SMTP_APP_PASSWORD` are *bearer strings* — they
authenticate from any IP, for anyone holding them. The same backup media that
was low-consequence for a deploy key is not low-consequence for these.

**The real controls are therefore not file permissions:**

- a **monthly spend limit** on the Anthropic key, set in the console — the
  only measure that binds someone using the key off this machine. The
  in-repo `AI_DIGEST_COST_CEILING_USD` does not: it is config on the same
  disk as the key and guards against a runaway loop in our own code, nothing
  more.
- the iCloud **app-specific password**, revocable at appleid.apple.com
  **independently of the Apple ID password** — revoking it changes no
  password and signs no device out. That independence is what makes it the
  safe credential to hand a Pi.
- keeping `.env` out of git — `git log --all --oneline -- .env` must be
  empty; a hit means the value reached GitHub and must be rotated before
  anything else. This is a pre-flight step in the runbook.
- accepting that **rotation, not deletion, is the remedy.** The 30-snapshot
  rotation means a value removed from `.env` today still sits on the flash
  drive for up to 30 days. Revoking at the vendor takes effect instantly;
  scrubbing backup media does not.

Full detail — the backup-exposure note, the pre-flight commands, and a
credential-recovery table listing the five variable names, where each is
obtained and how each rotates (names and sources only, never values) — is in
`docs/stage5-pi-deploy.md` §§ 3 and 6 and its "Credential recovery" section.

`pipeline/config.py` calls `load_dotenv()` unconditionally, but that's a
no-op if `.env` doesn't exist or doesn't set a given key -- python-dotenv
never overwrites a variable already present in the environment, so the Mac
wrapper script's Keychain-sourced env var always wins over anything (or
nothing) in `.env` on that machine.

## Email delivery

SMTP via iCloud, app-specific password. **Keychain on the Mac
(`ai-digest-smtp-username`, `ai-digest-smtp-app-password`), `.env` on the
Pi** — same split as the Anthropic key, per the Secrets section above.
There is no `.env` on the Mac.

Chosen over a transactional API (Postmark/SES/etc.) because it's one
email/week — not
enough volume to justify a new vendor account/dependency, and iCloud is
already the trusted core of this setup (it carries the vault sync). Known
risk: iCloud SMTP can be finicky about an unfamiliar Pi outbound IP; if that
ever bites, fall back to a free-tier transactional API rather than
re-architecting. (That risk is what the 4xx retry policy in `send.py` is
for — greylisting and IP-reputation throttling clear over minutes, hence the
deliberately wide 5s → 30s → 120s gaps.)

**Transport settings, all env-overridable** (`AI_DIGEST_SMTP_*`):
`smtp.mail.me.com`, port **587 with STARTTLS** — Apple's documented config.
465/implicit-SSL reportedly works too; flip `AI_DIGEST_SMTP_USE_SSL=true` and
the port if 587 misbehaves.

**VERIFIED 2026-08-21 — 587/STARTTLS works.** First real send from the Mac
was accepted on attempt 1, no retries, no 550 on `From`. Use these settings
on the Pi; if the Pi's outbound IP causes trouble it will show up as a 4xx
(the retry policy's job), not as a config problem.

**Delivery gotcha, cost ~20 minutes to diagnose:** the message was accepted
and delivered, but **did not appear in Mail.app** — only in the web client at
icloud.com. Self-sends (From and To both the same iCloud address) are
unreliable to surface in the desktop client. This matters for the Pi, where
nobody watches a console and "no email arrived" is the only symptom you'd
ever see. **Before concluding a send failed, check icloud.com in a browser
and check Junk there, not just Mail.app.** Better still, set `AI_DIGEST_TO`
to an address that is *not* the sending account.

`AI_DIGEST_FROM` must be an address the iCloud account actually owns, or
iCloud answers with a hard 550 (not retryable). It defaults to
`SMTP_USERNAME`, which is the full Apple ID and therefore definitionally
owned. An unrecognised `From` is the likeliest first-run failure — check it
before blaming TLS.

## Seen-set commit rule (built 2026-08-20, but the persist is GATED OFF)

`dedupe()` never persists — the caller commits. `deliver.py` is now that
caller and implements the rule below in full, **but the final `seen.save()`
is skipped unless `CONFIG.commit_seen` is on** (env `AI_DIGEST_COMMIT_SEEN`,
flag `--commit-seen`, default false). Items are still marked in memory, so
the code path is exercised and the counts are real; exactly one write is
skipped, and the run logs loudly that it did.

Why gated: score-stage failures at 15–23%/run and the `max_survivors` cap
both still discard content. While nothing persists, those defects merely
defer an item to next week. Persisting converts both into permanent
deletion. Fix them, re-run the UAT, then turn this on.

**Status of that precondition, 2026-08-20:** both causes now have fixes in
the tree — the round-robin cap fix, and the two score-stage fixes in
"Score-stage failures: what they actually were" above. **Neither has been
verified against a real run**, because the account ran out of credit. Keep
the gate off until a clean, credit-funded `--sync` run shows a score-failure
rate near zero. The rule itself: 

**Mark seen = fuzzy-dropped duplicates + filter-rejected items +
successfully-scored items.** Equivalently: everything *except* "the filter
said yes and we never got a score."

**Why mark rejects at all.** Not because of compounding cost — for a *dated*
item recurrence is bounded (10-day `fetch_max_age_days` at a weekly cadence
means at most one repeat), and a returning reject only re-enters the
*batched* filter stage, never the per-item score stage. Pennies. The real
reason is **undated items**: `filter_recent()` passes anything with no
published date through unconditionally, and listing-scraped sources almost
never carry a date. For those the seen-set is the *only* thing preventing
indefinite resubmission, with no upper bound. The docstring in
`fetch_strategy.py` already says so ("the persistent seen-set and stage 3's
filter are the backstop for those instead").

**Why the never-scored are excluded.** `parse_score_results` fails closed —
an errored or unparseable response drops the item silently, and there is a
documented incident where 22 of 27 responses were lost to `max_tokens`
truncation. Those items were judged *worth scoring* and then dropped by a
bug; marking them would permanently bury content with no symptom.
Filter-rejected items, by contrast, were genuinely judged.

**`max_survivors`-capped items are a DIFFERENT case — do not lump them in
here.** An earlier version of this rule did, on the premise that the cap
cuts "for reasons unrelated to merit" and so those items deserve another
shot next week. Verified 2026-08-20; the premise is wrong in a way that
inverts the conclusion. `run.py:179` is `passed[:CONFIG.max_survivors]`, and
`passed` is in **candidate order = source order** (`sources.tsv` row order,
with manual-only sources appended last), because `FilterVerdict` carries
only a pass/fail bool — there is no score to rank by. So the cap does not
cut arbitrarily, it cuts **positionally and deterministically**: the same
tail of the source list loses every single week. A source past the cutoff
never gets "another shot," so leaving its items unmarked doesn't preserve a
chance — it guarantees the unbounded weekly resubmission this whole rule
exists to prevent (see the undated-item argument above).

**The cap binds on every full run** (measured from `logs/`, counting API
calls per stage): 2026-08-16 → 12 filter requests (441–480 candidates) → **60
score requests = exactly `max_survivors`**; 2026-08-15 → 60 as well. Only the
`--limit-sources` run of 2026-07-18 came in under (9).

**Consequence, and it is worse than the seen-set question:** the 60 survivors
of the Aug 16 run run out around source row 14 (Hacker News). The six
manual-only feeds added specifically to fix the delivery-economics gap —
Economist ×3 and WSJ ×3 — sit at rows 46–51 and were **never scored at all**.
Fixing the cap's ordering comes before deciding how to mark its casualties;
`docs/stage4-send-plan.md` §0.7 carries the proposal.

**Related open question — the cheap filter is far more permissive than the
design assumed.** Aug 16: **312 of 441 candidates passed (~71%)**, against a
design intent of roughly 50 survivors reaching the expensive stage. That is
why the cap binds so hard: `max_survivors` isn't a rare circuit-breaker, it
is the load-bearing cost control, and the filter is barely filtering. Two
readings, not yet distinguished: the filter prompt is too lenient, or
title + 300-char excerpt genuinely isn't enough signal to reject on (the
stage is deliberately "permissive, not precise", and it fails open). **Not
being fixed now** — round-robin interleaving (§0.7) makes the cap's cut
fair, which is the urgent half. Tightening the filter changes what gets
scored at all and wants its own UAT pass. Recorded so the ~71% isn't
rediscovered as a surprise.

**A failed run needs no retry machinery.** Not committing the seen-set *is*
the retry: the items were never consumed, so the next run re-ingests them
normally. No dead-letter queue, no `--resume`, no state file.

**But a degraded run must not commit.** A scoring stage that mostly failed
produces an empty selection that is byte-identical to a genuinely thin week.
These need opposite handling — a thin week commits (those items *were*
judged), a degraded run must not (they weren't). The plan in
`docs/stage4-send-plan.md` gates on the score-failure *rate*, checked before
the empty-selection branch.

## Scheduling

Monday ~6am Pi-local (not Sunday evening) — picks up weekend publications,
lands at the start of the work week instead of during weekend wind-down.

## Reminders TSV format (confirmed from dump_sources.applescript)

List name: "Daily Digest". Header row `title\tnotes`. `title` = source name.
`notes` = reminder body, flattened to one line (newlines/tabs → spaces) —
in practice this is a URL, sometimes with trailing free text. Written to
`~/sources.tsv` on the Mac; the Pi-side rsync target is `data/sources.tsv`
relative to this project (overridable via `AI_DIGEST_SOURCES_TSV`).

## Reminders' dedicated URL field is a dead end -- don't re-investigate

Reminders.app has a separate "URL" field (distinct from Notes) visible in
the UI, and some of the 46 sources have their link stored there instead of
in Notes. **This field cannot be read by any method, because Apple never
persists it to the syncable record in the first place:**

- AppleScript's Reminders dictionary has no URL property at all.
- EventKit's `EKReminder.url` is a confirmed long-standing Apple bug: always
  nil on read, even via the official framework
  ([forum thread](https://developer.apple.com/forums/thread/128140)).
- Proven directly against this vault's own data: decoded the NSKeyedArchiver
  CKRecord blob (`ZCKSERVERRECORDDATA`) in the local Reminders SQLite store
  (`~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/
  Data-*.sqlite`, table `ZREMCDREMINDER`) for a reminder ("Jefferson
  Phisher") known to have a URL visible in the UI. The archive's `$top['URL']`
  key resolved to `$null`, and a brute-force scan of every string in the
  archive's object graph found zero URL/http substrings anywhere. The value
  is not stored locally in any form, synced or not -- there is nothing for
  any script to extract.

**Resolution:** the URL must live in the Notes field, which the existing
`dump_sources.applescript` + `pipeline/ingest.py` already read correctly.
This requires a one-time manual fix (copy each existing URL-field link into
Notes) and a workflow change going forward (paste new source links into
Notes, not the URL field) -- not a code fix. Diagnostic scripts used to
confirm this live in `scripts/inspect_reminders_db.py` and
`scripts/inspect_one_reminder.py`, kept in case a future macOS update is
ever worth re-checking against, but this path shouldn't be re-attempted
without new evidence.

## Status

**At a glance (Aug 21, 2026):** **Stages 1–4 COMPLETE and verified end to
end.** A real digest was sent to a real inbox on 2026-08-21: SMTP accepted on
attempt 1, email delivered, `outbox/Digests/🗞️ AI Digest 2026-08-21.md`
written with the correct final name and no `.partial` left behind. 243 tests,
all offline. **Measured cost of a full `--sync` run: $0.64** (60 Sonnet score
calls $0.539 + 13 Haiku filter calls $0.104) — under the $0.80–1.00 estimate,
and batch would roughly halve it. The ~$40–55/year projection holds.

Score-stage failures ran at **1 of 60 (1.7%)** on the last two runs, down
from 48% before the Aug 20 fixes.

**Stage 5a (Pi deploy) BUILT 2026-08-26** — see `docs/stage5-pi-deploy.md`
and `deploy/`. Stage 5b (Mac launchd/rsync glue) remains.

Remaining known gaps:

- **The seen-set is not persisted** (`commit_seen` ships off, deliberately —
  see Stage 4b–d below). Every run therefore re-ingests everything.
- **Presentation is now free to iterate on** — `--render-only` replays the
  last run's scored items offline, so a header/layout change no longer costs
  a ~$0.64 re-run to see against real content. See the Replay section below.
- ~~**Score-stage failures run 15–23% per run**, undiagnosed~~ —
  **DIAGNOSED AND FIXED 2026-08-20.** The Aug 20 log settled it, and the
  headline number was measuring two unrelated things at once. See "Score-stage
  failures: what they actually were" below.
- ~~**The org section skews AI-industry macro** rather than
  delivery-economics; "that content is genuinely scarce week to week."~~
  **RESOLVED 2026-08-21 — the scarcity explanation was FALSE.** It was three
  stacked bugs, not a content shortage: (1) `max_survivors` cut in source
  order and the Economist/WSJ feeds sit at rows 46–51, so they were never
  scored; (2) thinking blocks ate the score-stage token budget, truncating
  responses mid-JSON; (3) the Aug 20 run exhausted the API credit balance
  partway through scoring. With all three fixed, the Aug 21 UAT run put
  **three genuine delivery-economics items in the five org slots**: a WSJ
  piece on PE firms embedding AI specialists into portfolio companies (with a
  so-what about shrinking advisory revenue), AT&T shifting 40% of workloads to
  open models for 56% cost savings, and Benedict Evans on why job-exposure
  forecasts are a poor basis for pyramid restructuring. **Do not re-derive
  the scarcity conclusion — the rubric was never the problem.**
- **The filter passes ~71%** (312 of 441) against a design intent of ~50
  survivors — open question, see the Seen-set commit rule section. This is
  *why* the `max_survivors` cap binds every week and does the real curating.
- **One score failure per run, and it's a JSON-escaping bug.** The Aug 21
  drop ("📈 Data to start your week") was a complete, well-formed judgment
  that failed `json.loads` because the model wrote literal double quotes
  inside a string value: `shifting budget toward cheaper, "good enough"
  models`. Not truncation (`stop_reason=end_turn`, `blocks=('text',)`). Fix
  is either a prompt instruction to avoid double quotes inside string values,
  or — better — move the score call to structured output / tool use so the
  API enforces valid JSON instead of the model hand-writing it.
- **Fetch-layer losses are now the biggest content leak, not scoring.** The
  Aug 21 log shows: Economist **403** on every article fetch, WSJ **401** on
  every article fetch (both feeds work; it is the full-text fetch that is
  paywalled), HBR still `SSL: UNEXPECTED_EOF_WHILE_READING`, A16Z feed
  **404**, Intelligence Squared **410 Gone**, Azure feed timeout. The
  business-press sources are therefore being scored on feed excerpt alone
  while other sources get 4,000 words of article text — a systematic
  disadvantage. A16Z and Intelligence Squared are simply dead URLs in
  `manual_sources.tsv` and should be fixed or dropped.
- ~~**Stage 5 (Pi deploy + Mac glue) not started.**~~ **Stage 5a built
  2026-08-26** (systemd unit + timer + bootstrap + runbook). Stage 5b (Mac
  glue) still not started, so `data/sources.tsv` on the Pi is a static
  snapshot and `outbox/Digests/` accumulates unarchived notes.

**The decision that gated stage 5 — RESOLVED 2026-08-26.** `commit_seen` was
held off pending two things, and both are now met: score-stage failures are at
1 of 60 (1.7%), and the `max_survivors` round-robin fix (commit `cf0118c`) is
verified — `logs/run-20260821-162436.log` shows WSJ and Economist articles
reaching the full-text fetch, which only happens to items that survived the
cap. Those sources sit at rows 46–51 and had never been scored before. The
runbook therefore turns `AI_DIGEST_COMMIT_SEEN=true` on at deploy. Escape
hatch if it misbehaves: `rm cache/seen.json` restores a blank slate.

**Still true, and now sharper:** the filter passes ~71%, so `max_survivors` is
the real curator, and with `commit_seen` on its casualties are permanently
dropped rather than deferred a week. That is the next thing to fix.

## Score-stage failures: what they actually were (settled 2026-08-20)

The 15–23%-per-run failure rate, and the 48% on the Aug 20 `--sync` run, were
never one defect. `logs/run-20260820-182636.log` breaks down as **29 failures
of 60 requests (48%)**, and only two causes are present:

| Cause | Count | Nature |
|---|---|---|
| `400` — "Your credit balance is too low" | **28** | account-level, not per-item |
| `stop_reason=max_tokens` mid-JSON | **1** | real truncation |
| anything else | **0** | — |

Requests `score-0` … `score-31` succeeded; the balance hit zero and
`score-32` … `score-59` bounced instantly, all inside 18:34:13. **There is no
third or fourth cause in that log** — every one of the 28 carries the identical
billing message and `request_id`-per-line, and 31 items scored cleanly. The
earlier "56 of 60 / 3 unclassified" reading counted the whole queue as failed;
the run was closer to half-successful than that, right up until the money ran
out.

Two fixes landed, plus one for a reporting gap the incident exposed:

**1. An account-level 400 now aborts the run** (`pipeline/api_errors.py`, new).
`parse_score_results` fails closed, which is correct for a malformed answer
about one article and wrong for a failure that will hit every remaining
request identically. The pipeline walked the rest of the queue firing requests
that could not succeed and logged 28 ordinary drops. `is_fatal_api_error()`
now classifies: **fatal** = 401 auth / 403 permission / 404 bad model id / 400
whose body names a billing or quota refusal; **per-item** = everything else,
explicitly including 429 and 5xx (transient, already SDK-retried, and a
sustained outage is still caught by the degraded-run floor). `llm_client`
raises `FatalAPIError` at the first fatal error, in **both** the sync and batch
paths and for both stages — the same failure in the filter stage would have
been even more expensive to misread. `run.py` catches it and returns **exit 3**
(new code, distinct from 1 = send failed) before `deliver()` is ever called,
so no send, no `outbox/` write and no seen-set commit happen by construction
rather than by a flag. The billing detection is a message-substring match
because the API reports a zero balance as `invalid_request_error` — the same
error *type* as "prompt too long" — so nothing structured separates them. That
match is an optimisation over a safe default, not the only guard: if Anthropic
rewords the message, the failures degrade back to per-item drops, the rate goes
over the ceiling, and `deliver.py` still refuses to send or commit.

**2. Extended thinking is disabled on the score call.** The one genuine
truncation logged `blocks=('thinking', 'text')` — the model emitted a reasoning
block, those tokens are billed as output and count against `max_tokens`, and
`llm_client` concatenates `.text` only. **That is the Aug 16 `out_tokens=1000`
vs `len=211` contradiction, now confirmed rather than hypothesised.** The score
model (`claude-sonnet-5`) thinks by default when no `thinking` parameter is
sent. `build_score_requests` now sets `disable_thinking=True` on every request
and `llm_client` forwards `thinking={"type": "disabled"}`. Disabling beats
raising the cap: cheaper (no reasoning tokens at output prices), predictable
output length rather than just more rope, and it removes the failure mode
instead of widening it. `score_max_tokens` therefore stays at 1000 (~2× the
observed ~474-token response). Only the score stage opts out — the filter runs
on Haiku 4.5, which has a different thinking API and doesn't think by default.
**This was not only a cost issue:** the truncated body was well-formed JSON cut
off mid-`summary` with `org_score: 42` already written, on the WSJ
Stripe/OpenRouter story. A real judgment was lost to the cap.

**Caveat for the next UAT:** every score produced before this change was
produced *with* thinking on. Disabling it changes the judgment substrate, not
just the token accounting — if scores drift noticeably when the rubric is
re-judged, suspect this first.

**3. The cost report now reaches the log.** CLAUDE.md requires every run to
print *and log* token counts and cost. It was `print()`-only, from inside
`_print_digest`, so the Aug 20 log — the one run where the money mattered —
carries no cost figure anywhere. On the Pi nobody reads the console, so the
requirement was unmet in production precisely when it was needed. `run.py`
emits it from a `finally`, covering every exit path: clean return, budget
ceiling, fatal API abort, unhandled exception.

**Neither fix has been verified against a real run** — the account had no
credit left when they were written. The next `--sync` run after a top-up is
the verification, and it is also the re-run of the rubric UAT.

Stage detail below.

**Stage 1 (ingest + dedupe) — built, logic verified, not yet pytest-clean.**

- `pipeline/config.py` — all paths env-var overridable, so the same code
  runs unmodified on dev Mac and Pi.
- `pipeline/ingest.py` — parses the TSV above into `Source` objects.
  Live-file-missing/unparsable/empty all fall back to
  `cache/sources_last_good.tsv` with an escalating warning (plain vs
  STALE past `AI_DIGEST_SOURCES_STALE_DAYS`, default 10). Raises
  `IngestError` only if neither live nor cache exist — a genuine
  cannot-run condition.
- `pipeline/dedupe.py` — `SeenStore` is a persistent JSON seen-set keyed by
  canonical-URL hash (tracking params/www/scheme/trailing-slash stripped
  before hashing). `dedupe()` also clusters near-duplicate titles
  (difflib, 0.90 threshold, matching the vault scripts' convention) within
  a single run for cross-source syndication. `dedupe()` never calls
  `seen.save()` itself — the caller persists only once a run is committed
  (e.g. after the email actually sends), so a crash mid-run doesn't burn an
  item's one shot at being shown.
- `tests/test_ingest.py`, `tests/test_dedupe.py` — full pytest coverage
  written, **not yet run**: this sandbox has no network access, so
  `uv sync` can't fetch even `pytest`. I ran the equivalent assertions by
  hand with stdlib-only imports and they pass. Run `uv sync && uv run
  pytest` for real green checkmarks before trusting this stage further.

`data/sources.tsv` now has the real 44-row export. 37 sources parse (title +
URL); 7 (Founders, David Senra, a duplicate TBPN row, Modern Wisdom, Price of
Glory, Harvard Business Review, National Geographic History) still have empty
notes and are silently skipped until their links get moved into Notes too.

**Stage 2 (fetch) — built, pure logic verified, I/O layer unverified.**

Split into two files on purpose:

- `pipeline/fetch_strategy.py` — all the actual decision logic, stdlib-only
  (no httpx/feedparser), so it's fully unit-tested in a sandbox with no
  network access. Hostname classification (`classify_by_hostname`),
  Substack URL pattern (`substack_feed_url` — handles both `*.substack.com`
  and the `open.substack.com/pub/<name>` cross-reader wrapper), RSS/Atom
  autodiscovery HTML parsing (`extract_alternate_feed_links`), YouTube
  channel-ID extraction (`extract_youtube_channel_id`), listing-page
  article-link heuristic (`extract_listing_links` — same-domain only,
  skips nav/header/footer, drops short/icon-only link text), and
  `StrategyCache` (JSON, keyed by source name, human-correctable,
  `fetch_strategy_max_age_days` re-probes stale entries unless manually
  overridden). Verified against every real hostname in `data/sources.tsv`,
  including the trickier ones: `x.com` → unsupported, both YouTube channel
  URLs → youtube, both Substack forms → correct `/feed` URLs, Hacker News →
  known feed, ordinary blogs → "unknown" (needs the I/O layer to probe).
- `pipeline/fetch.py` — the actual HTTP requests and feed parsing
  (`httpx` + `feedparser`). Deliberately thin: mostly "call the library
  correctly" rather than logic, on top of the tested decision layer above.
  **Not run even once** — `httpx`/`feedparser` aren't installed in this
  sandbox either (same no-network wall as `uv sync`). This needs a real
  `uv sync && uv run python -m pipeline.fetch` smoke test against actual
  sources on your Mac before it's trusted.
- Four sources need explicit handling beyond plain RSS: `Sebastian Mallaby`
  (x.com — marked `unsupported`, no free/reliable RSS exists for X profiles,
  not worth building something fragile for one source), two YouTube channels
  (resolved to a channel ID once via a page fetch, then read through
  YouTube's public Atom feed endpoint — no API key needed), and everything
  with no discoverable feed at all (Paul Graham's essays page, Bay Area
  Times, Gartner, iShares, etc.) falls back to the listing-link heuristic.
- Full article text extraction (trafilatura) is deliberately **not** in this
  stage — only title/url/published/excerpt get pulled here, for every
  candidate. Fetching and extracting full text is deferred to stage 3, and
  only for the ~50 items that survive the cheap filter pass, so we're not
  spending bandwidth (or eventually tokens) on the hundreds of items that
  get discarded anyway. Matches the cost-discipline requirement.

**Stage 2 real-world validation (two rounds against actual sources.tsv) —
found and fixed real bugs, not just missing coverage:**

1. **Dedupe clustered same-source recurring titles.** Exponential View
   reuses "📈 Data to start your week" as a section title across different
   weekly editions (different URLs, different content). Title-similarity
   clustering didn't check source, so it silently dropped 2 of every 3 real
   editions. Fixed: `dedupe()` in `pipeline/dedupe.py` now only clusters
   across *different* sources — cross-source syndication was always the
   actual intent.
2. **No recency filter.** OpenAI's RSS feed returned 1038 items on one
   fetch — full archive, not "this week." Added `filter_recent()` in
   `fetch_strategy.py`, applied in `fetch.py`, default cutoff 10 days
   (`AI_DIGEST_FETCH_MAX_AGE_DAYS`). Items with no known published date
   (most listing-scraped ones) pass through unfiltered — the seen-store and
   stage 3 are the backstop for those.
3. **`FEED_SUFFIXES` was missing `/feed.xml`**, so TechMeme fell through to
   a broken listing-scrape (its homepage is JS-rendered; static HTML only
   had sponsor-post chrome). Added the suffix, and hardcoded TechMeme in
   `KNOWN_FEEDS` directly since the real feed is now confirmed.
4. **Listing-scrape picked up boilerplate CTAs**, not just nav/footer: A16Z
   returned "Learn More" 25 times, iShares/Every returned "Skip to
   content", AI Courses returned login-page chrome. Added a boilerplate
   phrase denylist plus a rule dropping any link text repeated more than
   twice on the same page (real headlines don't repeat; CTAs do) —
   `_BOILERPLATE_PHRASES` and the `Counter`-based filter in
   `extract_listing_links()`.
5. **YouTube channel-ID extraction failed for one channel** (George
   Vetticanden's) while succeeding for another (Jefferson Fisher's) — same
   code path, different page structure. Added two more extraction patterns
   (canonical `<link>` and `og:url` meta tag, both standard regardless of
   channel size) alongside the original two. Not yet confirmed fixed —
   needs a re-run against the real page.

**Sources marked `unsupported` in `cache/fetch_strategy.json` with
`human_override: true`** (won't be silently re-probed or auto-corrected),
each with a `detail` field explaining why — all confirmed via two real
fetches, not scraping bugs:

- **WSJ CIO Journal** — JS-rendered, static fetch returns only CSS/chrome.
- **Gartner Insights** — 0 links extracted, likely JS-rendered + bot
  detection.
- **AI Courses** (learn.deeplearning.ai) — login-gated course catalog, not
  an articles page. Structurally the wrong content type for this pipeline,
  not just hard to scrape.
- **WSJ Print** — a print-replica front page (paginated newspaper viewer),
  not individual articles.
- **iShares** — a fund-family product/locale-picker page, no article
  content exists on it regardless of link-text filtering.

**Stage 3 (filter + score/summarize) — built, pure logic verified, LLM I/O
layer unverified.** Split into more files than stages 1-2, deliberately,
so the cost-critical logic is independently testable:

- `pipeline/llm_types.py` — `LLMRequest`/`LLMResult`, stdlib-only shared
  types. Exists purely so filter_stage.py/score_stage.py don't need to
  import the anthropic SDK to be testable.
- `pipeline/cost.py` — `CostTracker`: accumulates real token usage,
  computes cost from a hardcoded pricing table (Sonnet $3/$15 per M
  tokens, Haiku $1/$5, batch = flat 50% off both — checked against
  platform.claude.com pricing docs, July 2026), and `check_budget()` is a
  **pre-flight** check called before every batch submission — a call that
  would exceed `CONFIG.cost_ceiling_usd` (default $5/run) raises
  `BudgetExceededError` before any spend happens, not after.
- `pipeline/filter_stage.py` — the module the "N candidates → 1 API call"
  requirement is actually about. `build_filter_requests()` packs up to
  `CONFIG.filter_batch_size` (40) candidates into each prompt, so it's
  structurally impossible for N candidates to produce more than
  `ceil(N/40)` requests — verified directly:
  `tests/test_filter_stage.py::test_n_candidates_never_produce_n_requests`
  runs this at N=1038 (the real OpenAI-feed count from stage 2 testing)
  and asserts 26 requests, not 1038. Filter verdicts **fail open**
  (unparsed/errored → passed through) since a missed item costs a few
  cents at the next stage, while a wrongly-dropped item is invisible —
  worse failure mode for a curation product.
- `pipeline/score_stage.py` — combined score+summary, one request per
  surviving item (never packed — each needs distinct full article text).
  Produces `ScoredItem` (org_score/org_reason, fluency_score/fluency_reason,
  2-sentence summary, so-what, vendor_marketing flag) from a single call.
  Unlike the filter stage, this **fails closed**: an unparsed/errored
  response drops the item rather than showing fabricated scores.
- `pipeline/select.py` — the two-objective selection. Top
  `select_org_count` (5) by `org_score` picked and removed first, then top
  `select_fluency_count` (3) by `fluency_score` from what's left. Test
  `test_protected_allocation_fluency_ranked_against_remaining_pool_only`
  proves the actual mechanism: an item with the highest fluency_score of
  the entire pool still gets excluded from "For you" because org claimed
  it first — this is what "protected allocation" concretely means, not
  just a design doc claim.
- `pipeline/llm_client.py` — the actual Anthropic Batch API calls
  (submit → poll `processing_status` → retrieve results), with prompt
  caching (`cache_control: ephemeral`) on the shared system prompt.

**First real end-to-end run (3 sources, `--limit-sources 3`) — worked, and
surfaced one real bug:** the whole pipeline ran (Keychain → filter batch →
article-text fetch → score batch → select → cost report, $0.19). But 22 of
27 score responses were dropped as unparseable. Root cause: `score_stage`
capped output at `max_tokens=500`, and Sonnet averaged ~474 out tokens/call
against that cap — i.e. many responses were being truncated mid-JSON and
failing `json.loads`, so the item got dropped. Because so few survived,
"For you" and "Considered and skipped" both came out empty (all 5 survivors
went to the org slots). Fixes:
  - `CONFIG.score_max_tokens` (new, default 1000) replaces the hardcoded
    500 — comfortable headroom for the full JSON object without materially
    moving cost.
  - `score_stage._extract_json_object` now falls back to a balanced-brace
    scan (quote-aware) so a response wrapped in prose still parses; a
    genuinely truncated object still fails closed (returns None), never a
    partial.
  - `parse_score_results` now logs `len`, `out_tokens`, and the raw tail of
    any response it can't parse, so a future recurrence is diagnosable from
    the log instead of a bare "could not parse".
- `pipeline/run.py` — wires ingest → dedupe → fetch → filter → score →
  select → render into one end-to-end run: prints the debug digest to
  stdout AND writes reader-facing previews to `outbox/` (see Stage 4a).
  Deliberately does **not** call `seen.save()` — the SEND half of stage 4
  doesn't exist yet, so nothing is actually delivered anywhere; committing
  the seen-set now would make items vanish from future runs without ever
  reaching an email.
- `pipeline/dedupe.py`'s `Candidate` gained `published`/`excerpt` fields
  (previously dropped after stage 1/2) since the filter stage needs the
  excerpt and stage 3 in general needs more than title/url/source.
  `candidate_from_raw_item()` converts stage 2's `RawItem` into this.
- Added `python-dotenv` loading to `config.py` (was documented as the
  chosen secrets approach but never actually wired up until now) — see
  `.env.example` for required keys (`ANTHROPIC_API_KEY` now,
  `SMTP_*` once stage 4 exists).

**Console/logging (added after the first run's output was an unreadable
wall of httpx INFO lines):** `run.py` now prints a clean 6-stage progress
view with an in-place counter for the slow parts (fetch, batch polling),
and routes all verbose detail to a timestamped `logs/run-*.log` (path
printed at the end). `--verbose` mirrors full detail to the console.
`fetch_all` gained an `on_progress` callback and `run_batch` an `on_poll`
callback purely to drive those progress lines (no effect on results).

**Stage 4a (render) — built.** `pipeline/render.py`: `render_email_html()`
(the reader-facing email — Delivery economics / Practitioner depth / Also
consider, each item with score, vendor_marketing badge, so-what for org,
links, HTML-escaped) and `render_vault_note()` (Obsidian markdown,
`type: ai-digest` frontmatter, `🗞️ AI Digest YYYY-MM-DD.md` filename). Pure
functions, fully tested (`tests/test_render.py`). Also `render_email_text()`
(the `text/plain` alternative part — no YAML frontmatter, since that's right
in a vault note and noise in an email body). Concepts frontmatter still TODO
(ScoredItem has no concept tags yet).

**Presentation pass, 2026-08-21 (render-only — scoring and selection
untouched).** Four changes, applied identically to all three formats:

- **Section headers renamed**, and each now carries a one-sentence
  description under it (muted, 12px against 14px body copy). The headers and
  blurbs are module constants — `ORG_HEADER`/`ORG_BLURB`,
  `FLUENCY_HEADER`/`FLUENCY_BLURB`, `TAIL_HEADER`/`TAIL_BLURB` — shared by
  HTML, plaintext and markdown so the three can't drift. `For the org` →
  **"Delivery economics"**, `For you` → **"Practitioner depth"**,
  `Considered & skipped` → **"Also consider"**. The blurbs are written from
  the *rubrics* (the `org_score` / `fluency_score` definitions above), not
  from the header names — "For you" in particular never said anything about
  the technical-depth-over-news-coverage bar it actually ranks on. If a
  rubric changes, change the blurb with it.
- **The tail items link**, in all three formats. It's a reading list; an
  unlinked title makes the reader go and search for it.
- **"Why it ranked" is no longer rendered anywhere.** This is a
  presentation decision, NOT a removal of the explainable-ranking spec —
  read it as such before "restoring" it. `org_reason` / `fluency_reason` are
  still produced by the score prompt, still carried on `ScoredItem`, and
  still printed by `run.py`'s console/debug digest, which is where a bad
  ranking gets diagnosed. What's gone is the per-item line in the *email and
  vault note*, where it largely restated the summary and pushed content down
  the page. **The score number stays in the meta line**
  ("org relevance 78/100"), so the ranking is still explainable to the
  reader. Re-adding the line is one line per format in `render.py`;
  `test_no_section_carries_a_reason_line_in_any_format` is what would need
  deleting, and it exists to make the removal deliberate rather than
  accidental.

**Stage 4b–d (send) — BUILT 2026-08-20, not yet run against real SMTP.**
Three new modules, all offline-tested:

- `pipeline/email_build.py` — pure MIME. `multipart/alternative`, plaintext
  FIRST and HTML SECOND (a client renders the LAST part it understands;
  reversing the order silently shows everyone the fallback). Subject is
  `AI Digest — Aug 24, 2026`, deliberately stable so Mail **threads** the
  archive — don't "improve" it by prepending the top headline.
- `pipeline/send.py` — SMTP transport. 3 attempts, 5s → 30s → 120s ±20%
  jitter, transient only. Never retries 535 auth / 550 sender-or-recipient
  refused / any 5xx: repeated bad-credential attempts against Apple risk
  throttling the account, turning a broken week into a broken month.
  `SMTPRecipientsRefused` is handled explicitly because it does NOT subclass
  `SMTPResponseException`, so the 5xx code check misses it and the `OSError`
  catch-all would otherwise retry it. Credentials are read from `os.environ`
  at point of use, never onto `CONFIG` (frozen dataclass, repr lands in
  tracebacks), and read BEFORE any socket opens so a missing password is an
  instant self-solving error naming the Keychain service.
- `pipeline/deliver.py` — the transaction and the degraded-run floor. Order:
  render → stage note to `.partial` on local disk → **send** → `os.replace`
  → commit. Disk before send, so a render/encoding fault aborts before an
  email is spent. **SMTP acceptance is the commit point**, so `os.replace`
  failing AFTER a successful send still commits (the alternative re-sends
  this week's stories next Monday, which is the worse failure) — and that
  path is plausible, not paranoid, because stage 5's Mac-side job clears the
  Pi's outbox and is a concurrent actor on that directory.

`run.py` gained `--apply`, `--to`, `--commit-seen`. Dry run is the default
and writes to `preview/` (gitignored), never `outbox/` — stage 5 sweeps
`outbox/`, so a preview left there would be archived as if delivered.
`--apply` is refused alongside `--limit-sources` (exit 2).

**`seen.save()` SHIPS DISABLED** — `CONFIG.commit_seen`, env
`AI_DIGEST_COMMIT_SEEN`, flag `--commit-seen`, default false. This is an
operational gate, not an unfinished feature: the whole commit path runs
(ordering, rollback, floor, mark set) and exactly one write is skipped,
logged loudly with the reason. **Why:** two known defects still discard
content silently — score-stage failures at 15–23%/run, and the
`max_survivors` cap until the round-robin fix has a UAT pass behind it.
(Both now have fixes in the tree as of 2026-08-20; neither is verified
against a real run yet. The gate stays off until one is.)
While the seen-set never persists, both merely defer an item a week; the
moment it persists, both become permanent deletion. Ship the email first,
fix those, then flip it on.

## Replay: `--render-only` (built 2026-08-21)

**Every run dumps its scored items to `cache/last_run_scored.json`, `--apply`
included; `uv run python -m pipeline.run --render-only` re-renders them with
no fetching and no API calls.** `pipeline/replay.py`.

**Why:** nothing about a run used to survive it. `ScoredItem` lived in memory,
the rendered previews were the only artifact on disk, and the logs carry a
response body only when parsing *failed*. So a digest that raised a question
("why did THAT rank first?") could not be re-examined once the process exited,
and every presentation change cost a full ~$0.64 re-run to see against real
content. The scored items are the only thing a run produces that costs real
money and can't be reproduced.

Decisions worth not relitigating:

- **The cache stores the INPUTS to `select()`** — the scored items plus the
  four Selection counts — never a rendered `Selection`. A replay therefore
  re-runs `select()` and reflects the *current* `select_org_count` /
  `select_fluency_count` / `skipped_cap`, so the cache is useful for tuning
  selection too, not only rendering. `select()` is pure, so an unchanged
  config reproduces the original digest exactly
  (`test_replay_reproduces_the_live_selection_exactly`).
- **The four counts are not optional.** They drive the footer's operator
  diagnostics ("9 of 60 could not be scored", "60 of 312 scored (cap)").
  Replaying without them renders a digest that looks *healthy* when the
  original run was not — silently converting the exact defect those lines
  exist to surface into an invisible one. `run.py` builds one `RunCounts` and
  passes the same object to both the cache and `select()`, so the two can't
  drift.
- **Written after the score stage, before `select()`.** Everything downstream
  is pure logic or retryable I/O; a crash in rendering or delivery must not
  take the scored items with it. `save_quietly()` swallows its own failures —
  it writes moments before an `--apply` run sends, and losing the cache costs
  one re-run while letting it abort the send costs the week.
- **`--render-only` refuses `--apply`, `--commit-seen`, `--limit-sources` and
  `--sync`** (exit 2). The first two are the safety property: the cache has no
  upper age bound, so `--apply` would mail out a fortnight-old digest and
  `--commit-seen` would permanently bury items on the strength of a replay.
- **Staleness is shouted, not just printed.** A replay's output is shaped
  identically to a fresh run — nothing in the digest says the news is old.
  Past `AI_DIGEST_REPLAY_STALE_DAYS` (default 7) the provenance block prints a
  loud warning, repeated after the digest because by then the first one has
  scrolled off the top of a 60-item debug dump.
- **`cache/last_run_scored.json` is DERIVED DATA and must stay gitignored.**
  It holds full summaries and scores for ~60 articles. `.gitignore`'s `cache/*`
  rule covers it; the two `!` exceptions next to it (`trust_tiers.json`,
  `fetch_strategy.json`) are the opposite kind of file — hand-edited config
  that happens to live in `cache/`. Do not add a third exception.
- `deliver._write_previews` became public `deliver.write_previews` so the
  replay path can call it without fabricating a `SeenStore` and `ScoreOutcome`
  to reach the one branch that ignores them.

**Operational note:** `--render-only` writes to `preview/`, overwriting
whatever is there. `preview/` is gitignored and has no backup, so if a past
run's preview is the only surviving copy of something, move it aside first (or
set `AI_DIGEST_PREVIEW_DIR`) — the Aug 20 previews were lost this way.

**The pipeline never touches the vault.** It writes markdown to `outbox/`
and stops; rsync-into-vault and clear-outbox are the Mac's job in stage 5,
because the Pi has no route to iCloud Drive — which is the whole reason the
architecture is split this way. Don't relitigate.

**Not started:** the manual smoke tests (they need Keychain entries and send
real email — see `docs/stage4-send-plan.md` §5), and stage 5b (Mac
launchd/rsync glue). Stage 5a (Pi deploy) was built 2026-08-26 —
`docs/stage5-pi-deploy.md`.

The design rationale behind all of the above — the questions, the rejected
alternatives, the test strategy — is in `docs/stage4-send-plan.md`.

## Stage 5a — Pi deploy (built 2026-08-26)

**Why this got built when it did:** the user expected a digest email on Monday
2026-08-24 and none arrived. The diagnosis was that **nothing had ever been
deployed** — the Pi was empty, no unit, no clone, no `.env`. Stages 1–4 were
complete and the 2026-08-21 send was a manual `--apply` from the Mac. There was
no bug; the launch mechanism simply did not exist. `git log` confirms no stage 5
commit before this one. **Do not go looking for a scheduling bug in the Aug 24
window — there was no scheduler.**

Artifacts: `deploy/ai-digest.service.template`, `deploy/ai-digest.timer`,
`deploy/bootstrap-pi.sh`, `docs/stage5-pi-deploy.md`. Both units pass
`systemd-analyze verify`; `OnCalendar=Mon *-*-* 06:00:00` normalizes correctly.

Decisions worth not relitigating:

- **`.env` is read by python-dotenv, NOT systemd `EnvironmentFile`.** Both
  could work, and CLAUDE.md's Secrets section names either. Using both means
  two parsers with different quoting rules on one secret file: systemd would
  hand Python an app-specific password with the quote characters still
  attached, surfacing as a 535 that looks like a wrong password. `config.py`
  already calls `load_dotenv()` unconditionally, so the unit just sets
  `WorkingDirectory` and lets it find the file. One parser.
- **`TimeoutStartSec=10800` is load-bearing, not decoration.** The default
  batch path polls up to `AI_DIGEST_BATCH_MAX_WAIT` (7200s), and systemd's
  default `TimeoutStartSec` for `Type=oneshot` is **90 seconds** — it would
  SIGTERM the run mid-poll every week, with no email and a misleading
  "timeout" in the journal.
- **`ExecStart` uses an absolute path to `uv`.** systemd's PATH excludes
  `~/.local/bin`, so a bare `uv` fails with `status=203/EXEC` — while working
  perfectly when tested by hand in a login shell. `bootstrap-pi.sh` resolves
  the real path and renders it into the unit, which is why the unit is a
  `.template`.
- **`LANG`/`LC_ALL=C.UTF-8` set explicitly.** The vault note filename starts
  with an emoji. PEP 538 would coerce C → C.UTF-8 anyway; relying on an
  implicit coercion for filename encoding is not worth the saving.
- **No automatic retry.** Exit 3 is fatal (billing/auth) and exit 1 means
  `send.py` already exhausted its 5s/30s/120s ladder. Repeated bad-credential
  attempts against Apple risk throttling the account.
- **`Persistent=true` on the timer.** A missed Monday is invisible — no email
  is the only symptom this system has — so catching up after downtime beats
  landing exactly on the hour.
- **The bootstrap refuses to arm the timer** while `.env` holds placeholders or
  `data/sources.tsv` is missing, rather than arming a unit that will fail at
  06:00 with nobody watching.

**The clone-and-run trap, worth knowing before debugging a fresh Pi:**
`.gitignore` carries `data/*` with only `manual_sources.tsv` re-included, so a
fresh clone has **no `data/sources.tsv`** — and the fallback
`cache/sources_last_good.tsv` is gitignored too. `ingest.py` raises
`IngestError` at stage 1 before any API call. The file must be scp'd from the
Mac, and until stage 5b exists it is a static snapshot that does not track
Reminders.

## Open items for next session

- **Org rubric broadened (Aug UAT decision):** after the strategy-only +
  trust-tier changes evicted the vendor how-tos, the org slots filled with
  AI-industry macro (lab valuations, DeepMind leadership churn, capex) —
  because the sources that carry true delivery-economics content (HBR, WSJ
  CIO Journal, Economist, Gartner) all fail to fetch (paywalled/JS), so the
  free pool skews to AI-industry news. **Partly superseded 2026-08-20:** WSJ
  and Economist now fetch fine — they are cut by the `max_survivors` source-
  order slice before scoring, which is a pipeline bug, not source scarcity.
  Re-run this UAT judgment once §0.7 lands; the rubric decision below may
  have been made against an artificially impoverished pool. User's call at
  the time: **AI-industry macro is fine.** `org_score` rewards two kinds:
  (a) delivery economics
  directly (highest), (b) AI-industry strategic context a delivery leader
  should track (solid). Still down-ranks implementation/how-to hard.
- **Future bolt-on (user idea, not started):** an "application to other
  industries" angle — surfacing how AI shifts play out in verticals beyond
  professional-services delivery. Park until the core digest is shipping.
- **Paywalled sources (in progress):** the delivery-economics content lives
  behind subscriptions the user pays for (WSJ, Economist, HBR, maybe
  Gartner).
  - **HBR — feed found, but currently BROKEN at fetch (SSL).** HBR publishes
    `https://feeds.harvardbusiness.org/harvardbusiness` (real articles,
    title+summary+link, each entry tagged with HBR subject categories +
    audience level like C-suite/CEO). Added to `manual_sources.tsv`, tiered
    `independent_analysis`. `fetch.detect_strategy` now checks "is the source
    URL itself already a feed?" before autodiscovery/scrape, so a direct-feed
    source works in principle. BUT the Aug 16 run showed httpx fails on this
    host with `SSL: UNEXPECTED_EOF_WHILE_READING` — a TLS-layer drop the
    browser-UA fix doesn't touch (see the run-outcome note below). Leading
    hypothesis: HBR's feed server is HTTP/2-only. Candidate fix:
    `httpx.Client(http2=True)` (needs `h2`) or `curl_cffi`. Deferred — the
    Economist covers the delivery-economics need, so HBR isn't blocking. The
    HBR *topic pages* are mostly the HBR store (paid case studies/books) —
    not useful; the feed is the articles. Future option: pre-filter feed
    entries by HBR's own `<category>` tags to save filter tokens.
  - **Manual sources mechanism (built).** `data/manual_sources.tsv`, merged
    in by `ingest.load_sources()` and NEVER overwritten by the Reminders
    rsync (which only touches `data/sources.tsv`). Same TSV format. On a name
    collision the **MANUAL entry wins** (flipped from the original
    Reminders-wins rule): the manual file is the user's deliberate override
    layer, used to replace a friend's bare-URL scrape entry with a real feed
    (e.g. "GCP" -> `cloudblog.withgoogle.com/rss/` overrides the Reminders
    "GCP" -> `cloud.google.com/blog`). Merge works even on the cache-fallback
    path (manual sources are independent of Reminders availability).
  - **Feed-over-scrape upgrades (found via feedspot + user).** Several
    Reminders "listing-scrape" sources turned out to have real feeds, added
    to `manual_sources.tsv` under the same name to override: GCP
    (`cloudblog.withgoogle.com/rss/`), AWS (`aws.amazon.com/blogs/aws/feed/`),
    A16Z (`a16z.com/feed/`), Intelligence Squared (Acast podcast feed
    `rss.acast.com/intelligencesquared`). GCP/AWS/A16Z stay `vendor`-tiered;
    Intelligence Squared `independent_news`.
    **Two of these are now DEAD (Aug 21 run):** `a16z.com/feed/` returns
    **404** and `rss.acast.com/intelligencesquared` returns **410 Gone**.
    Both need a working URL or removal from `manual_sources.tsv` — right now
    they cost a failed fetch every run and contribute nothing.
  - **Strategy cache now URL-aware (bug fix).** `cache/fetch_strategy.json`
    is keyed by source *name*; when a source's URL changes (scrape→feed), the
    old "listing" verdict would otherwise stick and the pipeline would try to
    scrape the feed URL as HTML. `StrategyCache.set` now stores the URL and
    `fetch_source` re-detects when it doesn't match (human overrides kept).
    Entries cached before this (url=None) harmlessly re-detect once.
  - **Economist — SOLVED via section RSS.** `economist.com/business/rss.xml`
    is a live public feed (user confirmed in browser) full of exactly the
    missing content ("how big a threat is AI to entry-level jobs", "why AI
    has not yet upset India's IT industry", "lessons from the frontiers of
    AI adoption"). Added to `manual_sources.tsv` as **"Economist (Business)"**
    — a DISTINCT name from the plain "Economist" the friend lists in
    Reminders (distinct because they're 3 different section feeds, not one).
    **Three sections now added** (all
    `economist.com/<section>/rss.xml`, all tiered `independent_analysis`):
    Business, Science & Technology, Finance & Economics. Confirmed via
    feedspot that every Economist section has a native `rss.xml`; the rest
    (Britain, Culture, Sport, regional) skipped as off-topic. NOTE: the AI
    *topic* page (`economist.com/topics/artificial-intelligence`) has NO
    native rss.xml — the Science & Technology section feed is the AI coverage.
  - **Browser User-Agent (changed for this).** `fetch.USER_AGENT` was an
    honest bot string; the Economist (and likely WSJ) 403 that even on public
    feeds. Changed to a normal Chrome UA — it's one weekly fetch of feeds the
    user subscribes to. Needed for the Economist feed to work from the Pi,
    since the site bot-blocked the old UA in stage 2.
  - **WSJ — SOLVED via section RSS.** Dow Jones hosts native feeds at
    `feeds.content.dowjones.io/public/rss/<name>` (NOT `feeds.a.dj.com` — the
    old guess; that's why it didn't render). Three added to
    `manual_sources.tsv`, tiered `independent_analysis`: WSJ (Business) =
    `WSJcomUSBusiness`, WSJ (Technology) = `RSSWSJD`, WSJ (Economy) =
    `socialeconomyfeed`. No CIO-Journal-specific feed exists (it's a
    newsletter), but Business + Technology cover the same enterprise-AI
    ground. NOTE: user's WSJ full-article access is limited (sub coded to
    a shared account) — but that only affects the user *reading* the linked
    article, not the pipeline *ranking* from the feed's headline+summary.
  - **EMAIL/IMAP ROUTE NO LONGER NEEDED.** Economist + WSJ work via public
    RSS, and HBR has a public feed too (just currently blocked at the TLS
    layer, see above) — so the whole dedicated-Gmail / newsletter-forwarding
    / IMAP ingestion plan is moot and was never built. Keep it in mind only
    if a *future* wanted source is truly email-only.
  - **Gartner — likely stays out** (gated research, no useful feed/newsletter).
  - **First real run WITH the business-press feeds (Aug 16) — outcome:**
    - **WSJ (all 3): fetch WORKS** — classified `rss`, fetching fine.
      ~~Just didn't crack the top this week.~~ **Corrected 2026-08-20: they
      never reached the scoring stage.** They are manual-only sources, so
      `ingest.load_sources()` appends them at rows 46–51, and
      `passed[:max_survivors]` cuts in source order at ~row 14. Same for all
      three Economist feeds. Nothing about their content was ever judged.
      See the Seen-set commit rule section and `docs/stage4-send-plan.md`
      §0.7.
    - **Economist (all 3): was BROKEN, now FIXED.** Root cause: `feedparser`
      does its OWN http fetch with a bot User-Agent — it never used the
      browser UA on the httpx client. The Economist 403'd feedparser even
      though httpx got a clean 200, so the direct-feed check failed and the
      feeds fell through to a broken listing-scrape of their own XML (cached
      as `listing`). Fix: `fetch._parse_feed()` now fetches every feed
      THROUGH httpx (browser UA, redirects, error handling) and hands the
      bytes to `feedparser.parse()` — feedparser never fetches on its own.
      `_feed_has_entries`, `fetch_rss`, `discover_feed`, `probe_feed_suffixes`
      all thread the client through now. Cleared the strategy cache (kept the
      6 human-override entries) so everything re-detects with the fix.
    - **HBR: still BROKEN.** `feeds.harvardbusiness.org` fails with
      `SSL: UNEXPECTED_EOF_WHILE_READING` on httpx — a TLS-layer drop, not a
      UA/403 (the browser-UA fix won't help). Both httpx and feedparser's
      urllib fail, but the browser-based web_fetch tool got the feed fine
      earlier. Leading hypothesis: the server is HTTP/2-only (browsers speak
      h2; httpx defaults to HTTP/1.1) or does TLS fingerprinting. Candidate
      fixes: `httpx.Client(http2=True)` (needs the `h2` dep) or a
      TLS-impersonating client (`curl_cffi`). UNVERIFIED — needs a real run.
      Economist covers the delivery-economics need, so HBR is not blocking.
- ~~Digest shows glued-on chrome titles for listing-scraped items~~
  **Fixed:** the score prompt now also returns `clean_title`, recovered from
  the article text it already has (no extra API call). `ScoredItem.title`
  prefers it and falls back to the raw scraped title if the model returns
  nothing/blank; `ScoredItem.raw_title` keeps the original. The prompt
  explicitly forbids inventing a headline, and `run.py` prints `(raw: ...)`
  whenever the title was rewritten so a UAT pass can spot an invented one.
- One listing-scraped URL 404'd ("Why AI apps fail in production") and got
  scored from nothing (org=5). Low priority — the model handled it
  gracefully — but listing scrapes can surface stale/moved links.
- George Vetticanden's YouTube channel is still `unsupported` even with the
  two added extraction patterns (canonical link, og:url) — genuinely
  couldn't debug further without live access to that channel's raw HTML.
  Low priority (one source out of 37).
- Several still-listing-scraped sources (Bay Area Times, Anthropic, Every,
  PineCone) return real headlines with page chrome glued on
  (GCP/AWS/A16Z/Intelligence Squared are no longer here — they got real
  feeds in `manual_sources.tsv`)
  (category label / byline / read-time concatenated with no separator,
  since the source HTML has no text node boundary between them). Not fixed
  — deliberately left as-is since stage 3 re-fetches full article text via
  trafilatura for anything that survives the cheap filter, which recovers
  the real clean title. Only matters if the glued-on noise ever turns out
  to confuse the cheap filter's judgment from title alone.
- 6 sources with empty notes still need their links moved from Reminders'
  URL field into Notes before they'll participate (Founders, David Senra, a
  duplicate TBPN row, Modern Wisdom, Price of Glory, National Geographic
  History). HBR was the 7th but is now handled via its feed in
  `manual_sources.tsv`, so it no longer needs the Notes fix.
