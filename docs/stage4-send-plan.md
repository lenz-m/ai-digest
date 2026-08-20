# Stage 4 (send) — implementation plan

Written 2026-08-20. Revised after review. For execution in Claude Code on
the Mac (network + real git available). Nothing here has been run — this is
a design document, not a verified build.

**Starting state:** stages 1–3 + 4a (render) built and working end to end.
133/133 tests pass. Repo versioned and pushed. `run.py` writes preview files
to `outbox/` and deliberately never calls `seen.save()`.

**Scope:** email send via iCloud SMTP behind `--apply`, the outbox note as a
committed artifact, and committing the seen-set once a run is actually
delivered — plus the three pre-existing defects in §0.3/§0.5/§0.6 that stage
4 would otherwise make dangerous.

**Not in scope:** stage 5 (Pi deploy, Mac launchd/rsync glue), the vault
note's `concepts:` frontmatter, the Media-stub stretch goal.

---

## 0. Corrections to CLAUDE.md and to the existing code

Six items. Four are documentation fixes; two are code defects that exist
today and become materially worse once the seen-set starts committing.

### 0.1 CLAUDE.md contradicts itself on who writes the vault — architecture wins

The **Architecture** section says:

> Raspberry Pi … writes finished `.md` to local `outbox/`.
> MacBook Air (launchd) … rsync Pi's `outbox/` → iCloud Obsidian vault, then
> clear it. The Pi cannot write iCloud Drive directly.

The **Status** section says stage 4 still needs:

> writing the vault note into the real iCloud vault + clearing outbox

Those describe different systems. Architecture is correct; the Status line
is a stale shorthand. **The pipeline never touches the vault.** It writes
markdown to `outbox/` and stops.

So the vault-write question is already decided: **outbox only.** Stage 4's
job there is narrower than the Status line implies — promote the outbox note
from "preview written on every dry run" to "committed artifact written only
on `--apply`", and stop mixing preview HTML into the directory stage 5 will
sweep.

### 0.2 The Email-delivery section names the wrong secrets store

> **Email delivery:** SMTP via iCloud, app-specific password in `.env`.

Predates the Keychain split and contradicts the **Secrets** section directly
below it. Should read "Keychain on the Mac, `.env` on the Pi." There is no
`.env` on the Mac and this plan does not create one.

### 0.3 The seen-set rule: what gets marked, and why

CLAUDE.md says "commit the seen-set after a successful send" without saying
*which* items. The seen-set is currently never written by anything —
`dedupe()` documents that the caller persists, and no caller does. Stage 4
has to decide.

**Rule: mark fuzzy-dropped duplicates + filter-rejected items +
successfully-scored items. Do not mark anything the filter passed that never
produced a score.**

Two independent justifications, and it matters which is which:

**(a) Why mark rejects at all — undated items, not compounding cost.**

An earlier draft of this plan claimed rejects would recur "every single week
forever — a permanent, compounding, silent re-spend." That was wrong, and it
contradicted this plan's own Q2. For a **dated** item the recurrence is
bounded: `fetch_max_age_days` is 10 and the cadence is weekly, so a dated
item can reappear at most once before ageing out. And a returning reject
only re-enters the *batched* filter stage (40 candidates per call), never the
per-item score stage. That is pennies, not a budget breach.

The real reason is **undated items**. Verified at
`pipeline/fetch_strategy.py:100`:

```python
return [item for item in items if item.published is None or item.published >= cutoff]
```

Items with no published date pass the recency filter **unconditionally**, and
the docstring names the consequence outright: *"The persistent seen-set and
stage 3's filter are the backstop for those instead."* Listing-scraped
sources almost never carry a date. For those items there is no ageing-out
mechanism at all — the seen-set is the *only* thing preventing indefinite
resubmission, week after week, with no upper bound. That is the justification.

(Can't currently quantify how many sources this covers:
`cache/fetch_strategy.json` holds only 6 entries because it was cleared after
the feedparser-UA fix. CLAUDE.md names Bay Area Times, Anthropic, Every,
PineCone and Paul Graham's essays page among the still-listing set — a
meaningful minority, not a corner case.)

**(b) Why exclude the never-scored — they were never judged.**

`parse_score_results` **fails closed**: an errored or unparseable response
drops the item silently (`score_stage.py:252-271`). There is a documented
incident where 22 of 27 responses were dropped to `max_tokens` truncation.
Marking those seen would permanently bury content on the basis of a
truncation bug, with no symptom.

The same argument covers a case the review didn't raise: `run.py:179` does
`survivors = passed[:CONFIG.max_survivors]`. Items the filter **passed** but
the cap then cut were never scored either. Filter-rejected items were
judged and found wanting; cap-cut and parse-failed items were judged
*worth scoring* and then dropped for reasons that have nothing to do with
their merit. Both belong in the never-marked set.

Hence the positive formulation above: everything except *"the filter said yes
and we never got a score."*

### 0.4 The Media-stub stretch goal implies an outbox layout decision

The stretch goal (`Media/🔗 <title>.md`) means outbox will eventually feed
**two different vault folders**. A flat outbox plus a flat rsync can't
express that.

Cheap now, expensive to retrofit: **make outbox mirror the vault's folder
structure** (`outbox/Digests/`), so stage 5's rsync is a plain mirror rather
than a router. Adding `outbox/Media/` later then costs nothing. Don't build
the Media stub.

### 0.5 No `encoding=` on any file I/O — pre-existing, fix all 11 sites

Verified: **zero** `encoding=` arguments across `ingest.py:80,124,149`,
`run.py:220,222`, `fetch_strategy.py:301,337`, `trust.py:115,129`,
`dedupe.py:103,122`. All fall back to `locale.getpreferredencoding(False)`.

Calibrated severity — smaller than it first looks:

- **The three JSON caches are immune.** `json.dumps` defaults to
  `ensure_ascii=True` — verified: every non-ASCII character in the payload
  comes back as a `\uXXXX` escape sequence, so the bytes on disk are pure
  ASCII. Those
  files are pure ASCII on disk, so read and write are safe under any locale.
- **The two genuinely at-risk writes are `run.py:220` and `:222`** — the HTML
  and markdown both carry `—`, `·` and 🗞️ literally. Plus `ingest.py`'s TSV
  reads if a source name is ever non-ASCII.
- **Filenames are safe regardless:** `sys.getfilesystemencoding()` is UTF-8
  with surrogateescape on Linux independent of locale, so the 🗞️ prefix is
  not the risk.

Reproduced to confirm it isn't purely theoretical:

```
LC_ALL=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0 → preferred: US-ASCII
UnicodeEncodeError: 'ascii' codec can't encode characters in position 17-18
```

Note what had to be disabled. On Python 3.12 under a normal Raspberry Pi OS
systemd unit, **PEP 538 locale coercion promotes `C` → `C.UTF-8`
automatically**, so this most likely won't bite. It bites only if something
sets a non-UTF-8 `LANG` explicitly or disables coercion. So: a latent risk
under an unusual-but-real locale config, not a guaranteed Pi failure.

Fix all 11 sites anyway — eleven one-word edits that retire the question
permanently.

Worth keeping in view: **the ordering in Q1 already contains this failure.**
The note is written to disk *before* the send, so a `UnicodeEncodeError`
there aborts the run before any email goes out and before `seen.save()`. The
ordering chosen for SMTP covers this too.

### 0.6 `filtered_out_count` conflates curation with breakage — and it's reader-facing

`run.py:211`:

```python
selection = select(scored, filtered_out_count=len(new_candidates) - len(scored))
```

That single number lumps together three unrelated things: filter rejects,
items cut by the `max_survivors` cap, and score-stage parse failures. And
`render.py:93` prints it to the reader as *"N more filtered below the cut."*

So today a scoring-stage breakage is reported in the email as successful
curation. On the first real run this would have read "22 more filtered below
the cut" when the truth was "22 responses were truncated mid-JSON."

**Fix:** split into `filtered_out_count` (filter rejects only, reader-facing,
unchanged wording) and `scoring_failed_count` (never scored).

Deliberate choice: `scoring_failed_count` goes to the **console and log,
loudly — not into the email body**. The reader doesn't benefit from pipeline
diagnostics in a curated digest, and the degraded-run floor (§1.7) already
gates the case where the number is large enough to matter. A small nonzero
count on an otherwise healthy run is a log concern.

---

## 1. Design decisions

### Q1 — What counts as "a successful send"?

**SMTP acceptance is the commit point. The email is the transaction; the
outbox note is not.**

| Rule | Failure it creates |
|---|---|
| Commit on SMTP accept | Email delivered, outbox note missing → no archive note this week. Recoverable by hand from the log. |
| Commit only when send **and** outbox write both succeed | Outbox write fails → seen not committed → next Monday re-sends **last week's stories** as a fresh digest. |

The second failure is worse and user-visible. A missing archive note is a gap
in a secondary channel; a duplicate digest is the product failing at the one
guarantee it makes. Email is designated primary in CLAUDE.md; the commit
point follows the primary channel.

**Ordering matters more than the rule:**

```
1. render                        pure
2. write note to a staging path  local disk — proves render + disk + encoding
3. send email via SMTP           the genuinely fallible step, LAST
4. os.replace() staging → outbox atomic rename
5. seen.save()
```

Writing to disk *before* the send means a render or encoding fault aborts
before an email is spent and before anything is burned. Next week retries
cleanly.

**Steps 4 and 5 are past the point of no return.** Once SMTP accepts, the
transaction has happened, and nothing after it may abort the commit — see
Q2b.

### Q2a — Note staged, send failed

**Roll back, log, exit non-zero, let the next run redo it.**

`seen.save()` never ran, so the items were never consumed. Next Monday
re-ingests them and they flow through normally. **Not committing the seen-set
*is* the retry mechanism** — no dead-letter queue, no `--resume`, no state
file. That sentence belongs in CLAUDE.md verbatim.

Roll back the staged note rather than leaving it: an undelivered run should
leave nothing in the directory stage 5 sweeps, or the vault gets a note for a
digest that was never received, then a second overlapping note next week.

Two bounded costs: the failed run's LLM spend isn't recovered (~$1 — cheaper
than a resume-from-cache path; don't build one), and consecutive failures
don't grow the pool without bound because `fetch_max_age_days` ages dated
items out. Undated items are the exception, per §0.3(a).

### Q2b — Email sent, `os.replace` failed

**Log loudly, then mark and save anyway.** The original pseudocode had this
backwards:

```python
os.replace(staged, dest)   # if this raises...
marked = _mark_all(...)
seen.save()                # ...never runs
```

The email is already out. Skipping the commit here produces exactly the
failure Q1 identifies as the worse one — next Monday re-sends this week's
stories. Q1 says the email is the transaction; the code has to implement
that.

**And this is more plausible than "rename basically can't fail":** stage 5's
Mac-side job *clears the Pi's outbox*. That makes the Mac a **concurrent
actor on the exact directory `os.replace` targets**. A clear-outbox landing
between the staging write and the rename yields `ENOENT` on a real schedule.

On failure, log the **full note content** at ERROR level, so Q1's claim that
the archive note is "recoverable by hand from the log" is actually true.

### Q3 — Testing the send path without sending real email

**An injected fake SMTP object for the whole suite. No local SMTP server in
pytest. One real self-addressed email as the manual smoke test.**

**(a) Pure message construction — no server, no sockets.** Split
`email_build.py` (builds an `EmailMessage`) from `send.py` (opens a socket).
Same split as `fetch_strategy.py` vs `fetch.py`, same payoff. Assert on the
object and on `msg.as_bytes()` round-tripped through
`email.message_from_bytes()` — that catches the encoding bugs that actually
bite (🗞️, em-dashes, quoted-printable of long HTML lines).

**(b) Transport with an injected fake.** `send_message()` takes an
`smtp_factory` defaulting to the real connector. Tests pass a `FakeSMTP`
recording `login`/`send_message`/`quit`, plus variants raising
`SMTPServerDisconnected` (proves retry) or `SMTPAuthenticationError` (proves
we do **not** retry). Inject `sleep` too, so backoff tests are instant.

**(c) `aiosmtpd` — recommended against for the suite.** Adds a dev
dependency, an asyncio loop and a listening socket, and proves only that
`smtplib` works — stdlib, not the code under test. Python's own `smtpd` was
removed in 3.12, so there's no zero-dependency version.

Keep the door open cheaply: make host/port/TLS-mode env-overridable, so
anyone who later wants a debug server points at `localhost:8025` with no code
change.

**The smoke test that matters:** `--apply --to <your own address>` against
real iCloud. Free, one minute, and proves the one thing no fake can — that
iCloud accepts this account, from this IP, with this `From` header.

### Q4 — Where SMTP credentials come from

**Two more Keychain entries in `scripts/run_with_secrets.sh`. No Mac `.env`.
The password never goes on the `CONFIG` object.**

Services, matching the existing `ai-digest-*` naming:

- `ai-digest-smtp-username`
- `ai-digest-smtp-app-password`

One behavioural difference from the Anthropic key: that one **hard-exits**
when missing. SMTP creds **warn and continue** — dry runs and `uv run pytest`
need no SMTP, and failing the wrapper without them would break the everyday
path. `send.py` raises a clear error at point of use when `--apply` is set
and creds are absent.

**Don't put the password on the `Config` dataclass.** It's a frozen
dataclass; its `repr` lands in tracebacks and is trivially printed while
debugging. Config holds non-secret transport settings; `send.py` reads
`SMTP_USERNAME` / `SMTP_APP_PASSWORD` from `os.environ` at call time.

`.env.example` gains the same keys as a committed template for the Pi. The
Pi's real `.env`, chmod 600, remains the only place a password lands on disk.

Keychain setup (once, in Terminal — `read -s` keeps it out of history):

```bash
printf "Apple ID for SMTP: " && read SMTP_USER && \
  security add-generic-password -U -a "$USER" -s ai-digest-smtp-username -w "$SMTP_USER" && \
  unset SMTP_USER

printf "App-specific password: " && read -s SMTP_PW && echo && \
  security add-generic-password -U -a "$USER" -s ai-digest-smtp-app-password -w "$SMTP_PW" && \
  unset SMTP_PW
```

Generate the app-specific password at appleid.apple.com → Sign-In and
Security. Then set "Confirm before allowing access" on both entries in
Keychain Access.app, matching the Anthropic key.

**iCloud specifics:**

- Host `smtp.mail.me.com`, port **587 with STARTTLS** is Apple's documented
  configuration; 465/implicit-SSL is widely reported to work too. Make the
  mode a config flag. Neither is tested against this account.
- `SMTP_USERNAME` is the full Apple ID.
- **`From:` must be an address the account owns.** An unrecognised `From`
  gets a hard 550, not a retryable error. Most likely first-run failure —
  check it before blaming TLS.

### Q5 — Vault note: direct write, or outbox?

**Outbox. Already decided — §0.1.** The Pi has no route to iCloud Drive;
that constraint is why the architecture is split this way at all.

What changes in stage 4 is the outbox *contract*. Today `run.py` writes both
`digest-preview-*.html` and the `.md` note into `outbox/` on every dry run.
Once stage 5 sweeps that directory, every stale preview gets copied into the
vault. Make the directory mean one thing:

- `outbox/` — **only** vault-bound files, written **only** by `--apply`.
- `preview/` — dry-run artifacts, overwritten freely, gitignored, swept by
  nothing.

That also makes the dry-run/apply distinction physically visible: a file in
`outbox/` means something was delivered.

### Q6 — Retry and backoff for iCloud SMTP

**3 attempts, 5s → 30s → 120s with jitter, transient errors only, wrapping
the SMTP transaction alone.**

**Retry:** `SMTPServerDisconnected`, `SMTPConnectError`, `SMTPHeloError`,
`socket.timeout` / `OSError`, and `SMTPResponseException` with a **4xx** code
(greylisting, rate limiting — the class CLAUDE.md's "finicky about an
unfamiliar Pi outbound IP" warning describes).

**Never retry:** `SMTPAuthenticationError` (535), `SMTPRecipientsRefused` /
`SMTPSenderRefused` (550), any 5xx. Retrying a 535 against Apple is worse
than failing — repeated bad-credential attempts risk account throttling or
lockout, turning a broken week into a broken month.

**Why the gaps are wide:** weekly job, zero latency pressure, so long waits
are free. Greylisting and IP-reputation throttling clear over minutes; a
1-second retry has approximately no chance, a 2-minute one has a real one.
Total added wall time ~3 minutes. ±20% jitter.

**Hard constraint:** the retry wraps the SMTP transaction **only**. It must
never re-enter render and categorically must never re-run an LLM stage — all
API spend has happened by then. Enforced by test (§5).

### Q7 — Degraded-run floor

**Gate on whether the machinery worked, not on whether the news was
interesting.** These need opposite handling, and today they're
indistinguishable.

**Refuse to send and refuse to commit when either holds:**

- `len(failures) == len(score_requests)` and `len(score_requests) > 0` —
  nothing scored at all. Unambiguous at any scale.
- `len(score_requests) >= 5` **and**
  `len(failures) / len(score_requests) > 0.30`

The 0.30 comes from the two observed data points, not from taste: a healthy
run's failure rate is ~0%, and the one real breakage was 22/27 = **81%**.
Anything above roughly a fifth is outside what a working stage produces, and
0.30 sits between the regimes rather than splitting the difference. The
`>= 5` guard stops a small `--limit-sources` run tripping on one odd
response.

**Deliberately excluded: a minimum selected-item count.** A week with 2 org
and 1 fluency item that all scored cleanly is a real thin week, and the
digest already says so ("Nothing cleared the bar this week"). Suppressing it
would delay real content by a week and hide a true signal about the source
pool. An item-count floor measures the news; the failure rate measures the
pipeline. Only the second should gate delivery.

**The two outcomes must diverge on the seen-set — this is the whole point:**

| | Send | `seen.save()` | Why |
|---|---|---|---|
| Thin week, scoring healthy | no | **yes** | Items were fetched, scored and judged unworthy. Re-judging next week is the §0.3(a) waste. |
| Scoring degraded | no | **no** | Items were never evaluated. Committing discards content on the basis of a crash. |

**The floor must be checked BEFORE the empty-selection branch.** A totally
failed scoring stage produces an empty `Selection` that is byte-identical to
a thin week (`select([])` returns empty lists for all three sections). Check
the empty branch first and a 27/27 failure silently consumes ~450 candidates
and sends nothing — symptom: a missing email.

---

## 2. Files to create and modify

**No new runtime dependencies.** `smtplib` and `email` are stdlib.

### New

| File | Contents |
|---|---|
| `pipeline/email_build.py` | Pure MIME. `subject_line()`, `build_digest_message(selection, generated_at, *, to_addrs, from_addr) -> EmailMessage`. `multipart/alternative`. |
| `pipeline/send.py` | Thin I/O. `SendError`; `_connect()` branching SSL vs STARTTLS; `send_message(msg, *, smtp_factory=None, sleep=time.sleep)`; credentials from `os.environ`. |
| `pipeline/deliver.py` | The transaction + the floor. `scoring_is_degraded()`, `deliver(...) -> DeliveryResult`. Owns ordering, rollback, commit rules. |
| `tests/test_email_build.py`, `tests/test_send.py`, `tests/test_deliver.py` | §5 |

`deliver.py` is separate from `run.py` on purpose: the commit-ordering rules
*are* the design decisions here, and `run.py` is 280 untested lines. With
`send_fn` injected, "send failed → seen not saved, outbox empty" is a
two-line test with no sockets.

### Modified

**`pipeline/score_stage.py`** — expose failures. Currently
`parse_score_results` returns `list[ScoredItem]` and every failure path hits
`logger.warning(...)` + `continue`, so callers cannot distinguish "rejected"
from "never scored". Prerequisite for §0.3(b) and Q7.

```python
@dataclass(frozen=True)
class ScoreFailure:
    candidate: Candidate
    reason: str          # "missing" | "errored" | "unparseable"

@dataclass(frozen=True)
class ScoreOutcome:
    scored: list[ScoredItem]
    failures: list[ScoreFailure]
```

The loop already distinguishes all three cases at the point of logging, so
this is ~10 lines. Cost is 9 mechanical test call-sites
(`tests/test_score_stage.py` lines 49, 65, 80, 165, 239, 250, 258, 266, 277 →
append `.scored`) plus `run.py:210`. No logic change.

**`pipeline/select.py` + `pipeline/render.py`** — split the count per §0.6.
`Selection` gains `scoring_failed_count`; `filtered_out_count` narrows to
filter rejects only. `render.py:92-93` wording unchanged (it now tells the
truth). `tests/test_select.py:93` and `tests/test_render.py:32` touched.

**`pipeline/config.py`**

```python
# --- Stage 4: email delivery ---
# iCloud SMTP. 587/STARTTLS is Apple's documented config; 465/implicit-SSL
# also reportedly works -- flip smtp_use_ssl and the port if 587 misbehaves.
smtp_host: str = os.environ.get("AI_DIGEST_SMTP_HOST", "smtp.mail.me.com")
smtp_port: int = int(os.environ.get("AI_DIGEST_SMTP_PORT", "587"))
smtp_use_ssl: bool = os.environ.get("AI_DIGEST_SMTP_USE_SSL", "").lower() in ("1", "true", "yes")
smtp_timeout_seconds: int = int(os.environ.get("AI_DIGEST_SMTP_TIMEOUT", "30"))

# Comma-separated (single recipient today; list keeps adding one env-only).
# digest_from must be an address the iCloud account owns or iCloud 550s.
digest_to: str = os.environ.get("AI_DIGEST_TO", "")
digest_from: str = os.environ.get("AI_DIGEST_FROM", "")

# Retry: weekly cadence, zero latency pressure -- wide gaps are free, and
# greylisting/IP-reputation throttling clears in minutes not milliseconds.
smtp_max_attempts: int = int(os.environ.get("AI_DIGEST_SMTP_MAX_ATTEMPTS", "3"))
smtp_backoff_seconds: str = os.environ.get("AI_DIGEST_SMTP_BACKOFF", "5,30,120")

# Degraded-run floor (Q7). Rate, not item count: this measures whether the
# scoring stage WORKED, not whether the news was interesting. 0.30 sits
# between a healthy run (~0%) and the one observed breakage (22/27 = 81%).
score_failure_rate_ceiling: float = float(os.environ.get("AI_DIGEST_SCORE_FAILURE_CEILING", "0.30"))
score_failure_min_sample: int = int(os.environ.get("AI_DIGEST_SCORE_FAILURE_MIN_SAMPLE", "5"))

# Dry-run artifacts. Kept OUT of outbox_dir: stage 5 rsyncs outbox/ into the
# vault, so a preview written there would be archived as if it were real.
preview_dir: Path = _path("AI_DIGEST_PREVIEW_DIR", "preview")
```

**Deliberately absent:** `SMTP_USERNAME`, `SMTP_APP_PASSWORD` — Q4.

**`pipeline/render.py`** — add `render_email_text()` for the plaintext part.
Reuse the markdown item helpers; omit YAML frontmatter (right in a vault
note, noise in an email body). Stays in `render.py` so all rendering is in
one tested module and `email_build.py` stays pure MIME.

**`pipeline/dedupe.py`** — make `mark_seen` idempotent:

```python
def mark_seen(self, candidate: Candidate, key: str | None = None) -> None:
    key = key or content_hash(candidate)
    if key in self._data:
        return  # keep the original first_seen -- re-marking must not reset it
    ...
```

Needed because the §0.3 set includes items already in the store.

**All 11 file I/O sites** — add `encoding="utf-8"` per §0.5.

**`pipeline/run.py`** — `--apply`, `--to ADDR`; **refuse `--apply` with
`--limit-sources`** (exit 2 — it would both send a junk digest and burn those
items); route dry-run artifacts to `preview_dir` and `--apply` through
`deliver()`; compute the §0.3 mark-seen set; non-zero exit on send failure or
degraded floor; update the docstring (it currently states stage 4 doesn't
exist and `seen.save()` is deliberately never called — both stop being true).

**`scripts/run_with_secrets.sh`** — warn-not-exit:

```bash
SMTP_USERNAME="$(fetch_secret ai-digest-smtp-username)"
SMTP_APP_PASSWORD="$(fetch_secret ai-digest-smtp-app-password)"
if [ -n "$SMTP_USERNAME" ] && [ -n "$SMTP_APP_PASSWORD" ]; then
    export SMTP_USERNAME SMTP_APP_PASSWORD
else
    # Not fatal: dry runs and pytest need no SMTP. --apply fails clearly at
    # point of use instead, so the everyday path keeps working without these.
    echo "note: SMTP creds not in Keychain -- --apply will fail. See docs/stage4-send-plan.md" >&2
fi
```

**`.env.example`** — `SMTP_USERNAME`, `SMTP_APP_PASSWORD`, `AI_DIGEST_TO`,
`AI_DIGEST_FROM`, placeholders only, commented as Pi-only.

**`.gitignore`** — add `preview/`.

### Outbox layout

```
outbox/
  Digests/
    🗞️ AI Digest 2026-08-24.md      <- --apply only; stage 5 mirrors this
preview/                              <- dry runs; gitignored; swept by nothing
  digest-preview.html
  digest-preview.md
```

Fixed preview filenames (not timestamped) so dry runs overwrite instead of
accumulating.

---

## 3. Reference sketches

### 3.1 `send.py` — retry loop

```python
_RETRYABLE = (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
              smtplib.SMTPHeloError, socket.timeout, OSError)

def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500      # 4xx transient; 5xx never
    return isinstance(exc, _RETRYABLE)
```

`SMTPAuthenticationError` and `SMTPSenderRefused` subclass
`SMTPResponseException` with 5xx codes, so the code check covers them — but
assert that in a test rather than trusting it. The ordering of those
isinstance branches is exactly what a later refactor breaks silently.

```python
def send_message(msg, *, smtp_factory=None, sleep=time.sleep) -> None:
    smtp_factory = smtp_factory or _default_factory
    backoffs = [float(s) for s in CONFIG.smtp_backoff_seconds.split(",")]
    last = None
    for attempt in range(CONFIG.smtp_max_attempts):
        try:
            with smtp_factory() as smtp:
                smtp.login(_require_env("SMTP_USERNAME"),
                           _require_env("SMTP_APP_PASSWORD"))
                smtp.send_message(msg)
            return
        except Exception as exc:
            last = exc
            if not _is_retryable(exc):
                raise SendError(f"permanent SMTP failure, not retrying: {exc}") from exc
            if attempt == CONFIG.smtp_max_attempts - 1:
                break
            delay = backoffs[min(attempt, len(backoffs) - 1)] * random.uniform(0.8, 1.2)
            logger.warning("SMTP attempt %d failed (%s), retrying in %.0fs",
                           attempt + 1, exc, delay)
            sleep(delay)
    raise SendError(f"SMTP failed after {CONFIG.smtp_max_attempts} attempts: {last}") from last
```

`_require_env` raises `SendError` naming the missing Keychain service and the
`security add-generic-password` command — this is the error a first `--apply`
most likely hits, so make it self-solving.

### 3.2 `deliver.py` — floor, then transaction

```python
def scoring_is_degraded(outcome) -> tuple[bool, str]:
    n = len(outcome.scored) + len(outcome.failures)
    if n == 0:
        return False, ""
    if len(outcome.failures) == n:
        return True, f"all {n} score requests failed"
    if n >= CONFIG.score_failure_min_sample:
        rate = len(outcome.failures) / n
        if rate > CONFIG.score_failure_rate_ceiling:
            return True, f"{len(outcome.failures)}/{n} score requests failed ({rate:.0%})"
    return False, ""


def deliver(selection, generated_at, *, send_fn, seen, mark_seen_candidates,
            score_outcome, apply: bool) -> DeliveryResult:
    if not apply:
        return _write_previews(selection, generated_at)

    # GATE 1 -- did the machinery work? MUST precede the empty-selection
    # branch: a fully-failed scoring stage yields an empty Selection that is
    # indistinguishable from a thin week, and committing it would bury ~450
    # never-evaluated candidates with no symptom but a missing email.
    degraded, why = scoring_is_degraded(score_outcome)
    if degraded:
        return DeliveryResult(sent=False, committed=False,
                              reason=f"scoring stage degraded: {why}")

    # GATE 2 -- genuinely thin week. Machinery fine, nothing cleared the bar.
    # Commit: these items WERE judged, and re-judging them is the §0.3 waste.
    if not selection.for_org and not selection.for_you:
        marked = _mark_all(seen, mark_seen_candidates)
        seen.save()
        return DeliveryResult(sent=False, committed=True,
                              reason="nothing cleared the bar", marked_seen=marked)

    dest = CONFIG.outbox_dir / "Digests" / vault_note_filename(generated_at)
    staged = dest.with_suffix(dest.suffix + ".partial")
    staged.parent.mkdir(parents=True, exist_ok=True)
    note = render_vault_note(selection, generated_at)
    staged.write_text(note, encoding="utf-8")

    try:
        send_fn(build_digest_message(selection, generated_at, ...))
    except Exception:
        staged.unlink(missing_ok=True)   # undelivered -> leave nothing behind
        raise

    # --- PAST THE POINT OF NO RETURN ---
    # The email is out. Q1 says the email is the transaction, so nothing
    # below may abort the commit: failing to save here means next Monday
    # re-sends this week's stories, which Q1 identifies as the worse failure.
    try:
        os.replace(staged, dest)
    except OSError as exc:
        # Plausible, not paranoid: stage 5's Mac-side job CLEARS the Pi's
        # outbox, so it is a concurrent actor on this exact directory and
        # can remove it between the write above and this rename.
        logger.error(
            "email SENT but outbox note could not be placed at %s: %s. "
            "Committing the seen-set anyway. Full note content follows so "
            "the archive entry can be recovered by hand:\n%s", dest, exc, note)

    marked = _mark_all(seen, mark_seen_candidates)
    seen.save()
    return DeliveryResult(sent=True, committed=True, note_path=dest, marked_seen=marked)
```

`.partial` keeps staging in the same directory so `os.replace` is
same-filesystem and genuinely atomic. Stage 5's rsync should still filter
`--include='*.md' --exclude='*'` as belt and braces.

### 3.3 `run.py` — building the mark-seen set

```python
# §0.3: everything EXCEPT "the filter said yes and we never got a score".
# Excluded: max_survivors-capped items and score-stage failures -- both were
# judged worth scoring, then dropped for reasons unrelated to their merit.
mark_seen_candidates = (
    dropped                                              # fuzzy dupes + already-seen
    + [v.candidate for v in verdicts if not v.passed]    # filter-rejected: judged
    + [i.candidate for i in outcome.scored]              # scored: judged
)
```

---

## 4. Answered questions

1. **Recipients: you only, plain `To:`.** `AI_DIGEST_TO` still parses as
   comma-separated so adding someone later is env-only, but there's no Bcc
   branch to maintain.
2. **Subject: `AI Digest — Aug 24, 2026`.** Rationale to preserve against a
   future "improvement": a stable subject **threads properly in Mail**, which
   matters more as the archive grows. Prepending the top headline would make
   each week a new thread.
3. **Degraded-run floor: yes** — Q7.

---

## 5. Test strategy

~30 new tests, all offline, all fast. Current suite 133 → ~163, no network,
no real sleeps.

### `tests/test_score_stage.py` — extend

- Failures are returned, not just logged: one missing + one errored + one
  unparseable → `len(outcome.failures) == 3` with the right `reason` on each.
- A fully-failed batch returns `scored == []` **and** three failures — the
  input to Q7's floor.

### `tests/test_email_build.py`

- Subject contains the date, single line, no CR/LF injection.
- `multipart/alternative`, exactly two parts, `text/plain` first and
  `text/html` second (clients render the last part they understand).
- HTML part byte-identical to `render_email_html(selection, now)`.
- **Round-trip:** `message_from_bytes(msg.as_bytes())` recovers 🗞️, an
  em-dash and a non-ASCII source name from both parts.
- A title containing `\r\n` cannot inject a header.
- Empty `for_you` still builds a valid message.

### `tests/test_send.py` — injected fake, zero sockets

- Happy path: `login` once with env credentials, `send_message` once, closed.
- Retries `SMTPServerDisconnected`, succeeds on attempt 3; factory called
  exactly 3 times.
- Exhausts → `SendError` after `smtp_max_attempts`.
- **No retry** on `SMTPAuthenticationError` — factory called exactly once.
  (The one protecting the account.)
- **No retry** on 550 `SMTPSenderRefused`; **does** retry 421.
- Backoff delays read from config and passed to injected `sleep` in order —
  asserted on recorded values, so the test takes 0ms.
- Missing `SMTP_APP_PASSWORD` → `SendError` naming the Keychain service,
  before any connection.

### `tests/test_deliver.py` — the important ones

Floor:

- All-failed outcome → `sent=False`, `committed=False`, **nothing marked**,
  `seen.save()` never called.
- 22/27 failures → degraded.
- 1/27 failures → not degraded, sends normally.
- 2/4 failures (50%, below `min_sample`) → **not** degraded — small-run guard.
- **Floor is evaluated before the empty-selection branch:** an all-failed
  outcome *with* an empty selection must return `committed=False`. This is
  correction 1 as an executable assertion; if the branches are ever
  reordered, this is the test that catches it.

Transaction:

- Dry run writes to `preview/`, never `outbox/`, never calls `send_fn`, never
  calls `seen.save()`.
- Apply success: note at `outbox/Digests/🗞️ AI Digest <date>.md`, no
  `.partial` left, seen saved.
- Apply, send raises: `outbox/` empty, no `.partial`, `seen.save()` never
  called.
- **Staged file exists at the moment `send_fn` is invoked** — asserted from
  inside the fake. This is what makes Q1's ordering true rather than
  aspirational, and it's what a refactor is most likely to break.
- **`os.replace` fails after a successful send → seen IS still saved**, and
  the note content appears in the log record. Correction 4 as a test.
- Empty selection + healthy scoring → no send, seen **saved**,
  `sent=False, committed=True`.

Mark-seen set:

- Filter-rejected and successfully-scored items are marked.
- **Score-stage failures are NOT marked.**
- **`max_survivors`-capped items are NOT marked.**
- Fuzzy-dropped duplicates are marked.
- Re-marking an already-seen item preserves the original `first_seen`.

Cost:

- **Delivery makes zero LLM calls:** pass a populated `CostTracker` through a
  delivery with 3 send attempts, assert `len(tracker.records)` unchanged.
  Stage-4 analogue of `test_n_candidates_never_produce_n_requests`.

### `tests/test_render.py` — extend

- `render_email_text` contains every item title and URL, and has **no** YAML
  frontmatter.
- `scoring_failed_count` does **not** leak into the email HTML (§0.6).

### Manual smoke tests (Mac, network, in order)

1. `./scripts/run_with_secrets.sh uv run pytest` — all green, no network.
2. Dry run → `preview/` populated, `outbox/` still empty, `cache/seen.json`
   mtime unchanged.
3. Guard: `--apply --limit-sources 3` → exits 2, sends nothing.
4. **Real send to self:** `--apply --to <your own address>`. Verify: arrives,
   renders in Mail.app *and* on iPhone, links work, emoji intact, plaintext
   part readable. Then `outbox/Digests/` has exactly one `.md`, no
   `.partial`, and `cache/seen.json` grew by the expected count — which is
   **not** the full candidate count, since never-scored items are excluded.
5. **Idempotence:** immediately re-run `--apply`. Should report nothing new.
   Best single end-to-end check that the commit worked.

---

## 6. Build order

Each step ends green; only step 8 needs network.

| # | Step | Why here |
|---|---|---|
| 0 | CLAUDE.md fixes §0.1–0.4 (docs-only commit) | Done before code, while the reasoning is loaded. |
| 1 | `encoding="utf-8"` on all 11 sites | Independent, mechanical, zero risk. Retires §0.5. |
| 2 | `config.py` additions, `.env.example`, `.gitignore` | Everything downstream reads config. No behaviour change. |
| 3 | `ScoreOutcome` + `scoring_failed_count` split (+ 11 test call-site updates) | **Prerequisite for §0.3(b) and Q7.** Must precede `deliver.py`. Mechanical, no logic change. |
| 4 | `render_email_text()` + `mark_seen` idempotence, with tests | Two small independent changes. Green before anything new exists. |
| 5 | `email_build.py` + tests | Pure. Biggest test payoff per line. |
| 6 | `send.py` + tests | Retry policy is real logic — test it with the fake before it touches a socket. |
| 7 | `deliver.py` + tests | The transaction and the floor. Composes 5 and 6. Budget the most care here. |
| 8 | `run.py` wiring: `--apply`, `--to`, the `--limit-sources` guard, preview/outbox split, mark-seen set, docstring | Glue only; all logic tested underneath. |
| 9 | Manual smoke tests §5, in order | First network. Expect the `From` 550 or a port/TLS issue here, not earlier. |
| 10 | Update CLAUDE.md Status with what actually happened | Especially the port/TLS combination that worked — the Pi deploy depends on that fact. |

Conventional commits (`feat:`, `fix:`, `test:`, `docs:`). Steps 0–8 are
Mac-local and reversible; step 9 is the first that sends anything real.
