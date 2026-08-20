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
`org_score` (delivery-economics relevance, for "For the org") and
`fluency_score` (AI-practitioner fluency, for "For you") — each with its own
one-line reason, never a bare verdict.

Selection order matters: top 5 by `org_score` are picked first and removed
from the pool, *then* top 3 by `fluency_score` are picked from what's left.
That removal order is what makes "For you" actually protected — if fluency
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
  `python-dotenv` / systemd `EnvironmentFile`.

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
re-architecting.

## Seen-set commit rule (decided, not yet built)

`dedupe()` never persists — the caller commits, and no caller does yet. When
stage 4 adds one, this is the rule:

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

**At a glance (Aug 2026):** Stages 1–3 built and working end to end; stage
4a (render) built. A full `--sync` run ingests 44 sources → ~450 candidates
→ filter → score → select → writes an HTML email preview + vault note to
`outbox/`, ~$1/run at full sync price (~half that on batch). Curation
machinery is doing its job: org section is all independent-analysis/news
(no vendor junk), fluency section is real practitioner content, ranking is
explainable (score + reason per item). Remaining known gaps: the org
section skews AI-industry macro rather than delivery-economics — **the
"content is genuinely scarce" explanation is at best partial**, because the
`max_survivors` cap cuts in source order and the Economist/WSJ feeds sit at
the end of the source list, so they have never once been scored (see the
Seen-set commit rule section); HBR feed blocked at TLS; and the whole
SEND/deploy half (stage 4 send + stage 5) not started. Detail below.

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
(the reader-facing email — For the org / For you / Considered & skipped,
each item with score + one-line reason per the "explainable ranking" spec,
vendor_marketing badge, so-what for org, links, HTML-escaped) and
`render_vault_note()` (Obsidian markdown, `type: ai-digest` frontmatter,
`🗞️ AI Digest YYYY-MM-DD.md` filename). Pure functions, fully tested
(`tests/test_render.py`). `run.py` now writes both to `outbox/` as previews
on every (dry-run) run and prints the paths — so the digest can be OPENED
and vetted before send exists. `outbox/digest-preview-*.html` is the one to
open. Concepts frontmatter still TODO (ScoredItem has no concept tags yet).

**Not started (rest of stage 4):** actual email SEND (iCloud SMTP,
`--apply`), promoting the vault note from a dry-run preview to a committed
`outbox/Digests/` artifact written only under `--apply`, and committing the
seen-set only after a successful send. Stage 5 (Pi deploy docs + Mac
launchd/rsync glue) also not started.

**Correction:** an earlier version of this line said stage 4 needed
"writing the vault note into the real iCloud vault + clearing outbox." That
contradicted the Architecture section and was wrong. **The pipeline never
touches the vault.** It writes markdown to `outbox/` and stops; rsync-into-
vault and clear-outbox are the Mac's job in stage 5, because the Pi has no
route to iCloud Drive — which is the whole reason the architecture is split
this way. Don't relitigate.

A written implementation plan for the rest of stage 4 — design decisions,
file-by-file changes, test strategy, build order — is in
`docs/stage4-send-plan.md`. Nothing in it is built yet.

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
