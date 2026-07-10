# Creator Signals — design & build plan

**Status:** Phase 0 (spike) ✅ done — building not yet started.
**Feature:** Automatically screen the stocks mentioned in a creator's new YouTube
videos. When a new video lands, fetch its transcript, extract the tickers
discussed (with stance), run them through the existing screener, and surface the
results on a page.

---

## Decisions (confirmed)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Creator | **ZipTrader** — `https://www.youtube.com/@ZipTrader`, channel_id `UC0BGhWsIbV7Dm-lsvhdlMbA` |
| 2 | Extraction | **LLM-primary (Gemini) + deterministic dictionary fallback** |
| 3 | Watchlist | **Display-only, with a one-click "add to watchlist"** (no auto-add) |
| 4 | Cadence | **Every 6 hours** |
| 5 | Transcript blocking | **Direct fetch + retry** to start; add a proxy only if the runner is blocked |

Design keys on `channel_id`, so adding more creators later is data, not code.

---

## Phase 0 spike — results (verified locally, 2026-07-09)

Run from the local (residential) IP:

- `@ZipTrader` → `UC0BGhWsIbV7Dm-lsvhdlMbA` (from the channel page's `<link rel="canonical">`).
- Channel RSS `https://www.youtube.com/feeds/videos.xml?channel_id=UC0BGhWsIbV7Dm-lsvhdlMbA`
  returns the latest ~15 videos. **It intermittently 404/500'd, then 200'd on retry** — the poll MUST retry.
- `youtube-transcript-api` (v1.x) `YouTubeTranscriptApi().fetch(video_id)` returned a
  full transcript (~25.6k chars) for a real ZipTrader video. Titles like "5 Stocks
  To BUY HEAVY For July 2026" confirm the content is stock-dense.

**Open risk:** all of the above worked from a residential IP. The **GitHub Actions
runner uses a datacenter IP**, where YouTube may block the transcript endpoint.
This is only provable by running the real workflow (Phase 4) — if blocked, add a
proxy (`YT_PROXY_URL`) or the YouTube Data API captions path.

---

## Architecture

Same producer/consumer shape as the warm-cache job:

- **Producer** — a scheduled GitHub Actions script (`scripts/scan_creators.py`)
  does the slow work: poll → transcript → extract → screen → write to DB.
- **Consumer** — a Streamlit page reads the stored rows; users never wait on it.
- Data is **global/shared** (everyone sees the same screens), so the new tables
  behave like `price_cache` / `news_cache`: no per-user RLS. Only "add to
  watchlist" is per-user.

### New components

| File | Responsibility |
|------|----------------|
| `engine/data_sources/youtube_client.py` | Provider layer: `latest_videos()` (Data API → RSS fallback), `resolve_channel()`, `get_transcript()` (Supadata → proxy → direct). Raw calls, no caching. |
| `engine/data_sources/youtube_data_api.py` | **Official Data API v3** (`YOUTUBE_API_KEY`): `list_uploads()` + `resolve_channel()`. Free — 1 quota unit/call, 10,000/day. ⚠️ Replaced the RSS feed, which throttles hard: **measured 1-in-6 success** (mixed 404/500), which failed whole scans. `channels.list?forHandle=` also resolves @handles without scraping the IP-blocked channel page. |
| `engine/data_sources/supadata_client.py` | Hosted transcript API (`SUPADATA_API_KEY`) — fetches server-side, so it works from the datacenter IPs YouTube blocks. |
| `engine/data_sources/sec_tickers.py` | Fetch + cache SEC `company_tickers.json`; build `{normalized_name → ticker}` + `{ticker}` maps. |
| `engine/ticker_extraction.py` | `extract_mentions(text) -> list[Mention]`. Gemini structured-output primary; dictionary + `$cashtag` fallback; then **validate** each candidate against a real quote. |
| `engine/creator_signals.py` | Orchestrator `scan_creators()`: new videos → transcript → extract → `screener.screen_tickers` → persist. Idempotent. |
| `db/models.py` (+ Alembic migration) | New tables (below). |
| `scripts/scan_creators.py` | Cron entry-point (mirrors `scripts/warm_cache.py`). |
| `.github/workflows/creator-signals.yml` | `cron: 0 */6 * * *` + `workflow_dispatch`, `concurrency: creator-signals`. |
| `app/pages/9_creator_signals.py` | The display page. |
| `app/_cache.py` | Cached reader for the page. |
| `requirements-signals.txt` | Minimal deps for the cron (see below). |

### Data model
```
creators          id, channel_id (unique), handle, display_name, active, added_at
creator_videos    id, creator_id FK, video_id (unique), title, url, published_at,
                  transcript_status (ok|no_captions|blocked|error), processed_at
video_mentions    id, video_id FK, ticker, company_name,
                  stance (bullish|bearish|neutral|unknown), confidence,
                  screener_score, recommendation, screened_at
```
`video_id` unique = dedup key (never reprocess). `transcript_status=blocked` rows retry next run.

### Pipeline (per cron run)
1. For each active creator: GET the channel RSS (with retry) → recent `{video_id, title, published}`.
2. Skip any `video_id` already stored. For each new one:
3. `get_transcript(video_id)` → text, or record `no_captions`/`blocked` and continue.
4. `extract_mentions(text)` → candidates `{ticker, company, stance, confidence}`.
5. Validate each ticker resolves to a live quote (drops "IT/CEO/DD/A" etc.).
6. `screener.screen_tickers([validated])` → score + recommendation.
7. Persist `creator_videos` + `video_mentions` in one transaction.

### Extraction (the hard part)
- **LLM primary (Gemini):** transcript → structured JSON
  `[{ticker, company, stance, confidence}]`. Quota-safe: runs **once per new
  video** (~0–2/day), 1 call each — the 20/day limit that hurt interactive chat
  is irrelevant here.
- **Deterministic fallback (free):** SEC name↔ticker map + `$cashtags` + a
  stop-list. Used when there's no key/quota. Mirrors the chat assistant's
  LLM-with-fallback pattern.
- **Validation gate (both):** every candidate must be in the SEC ticker list —
  the biggest false-positive killer.

**Phase 2 finding (real ZipTrader transcript):** the deterministic fallback was
badly noisy with single-word company-name matching — words the speaker uses
constantly ("bullish", "honest", "people", "pattern") are also real company
names, so a 25k-char transcript yielded ~16 tickers, most bogus. Fix: the
deterministic path now matches **only** $cashtags, explicit uppercase symbols,
and **multi-word** names — that cut the same transcript to a clean `[MBLY, META]`
(0 false positives) but with **much lower recall** (misses Tesla/Alphabet/… said
as lone lowercase words). So the LLM is not just "better" — it's **required for
good recall**; the deterministic path is a precise-but-sparse safety net for when
the Gemini key/quota is unavailable. Another reason to enable Gemini billing.

---

## Guardrails (honesty)
- Page reads *"stocks **mentioned** in ZipTrader's video"* — **mention ≠
  endorsement** (he may be bearish; that's why we capture `stance`).
- Screener scores keep the *"explainable score, not advice"* framing, same spirit
  as the news "context, not cause" line.

## Risks & mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| YouTube blocks transcript fetch from Actions IPs | **Med–High** | Retry next run; mark `blocked`; add proxy secret. Prove in Phase 4. |
| RSS transient 404/500 | **Observed** | Retry loop in `latest_videos` |
| Captions disabled on a video | Low–Med | Mark `no_captions`, skip (later: yt-dlp + faster-whisper) |
| Ticker false positives | Medium | Dictionary + stop-list + quote-validation gate |
| YouTube ToS (unofficial caption endpoint) | Low (personal use) | Noted; compliant path is Data API + OAuth |
| Gemini quota | Low here | Once-per-video; deterministic fallback |

## Testing
All offline: mock RSS XML + `youtube-transcript-api`; feed the deterministic
extractor fixed text + a tiny name map; mock the LLM path; mock screener + use the
in-memory SQLite conftest. Orchestrator tests: new videos process, dupes skip,
`blocked` retries.

---

## Phased rollout

| Phase | Deliverable | Status |
|------|-------------|--------|
| 0 — Spike | Prove RSS + transcript work | ✅ done (locally) |
| 1 | DB tables + migration + creator seed + orchestrator storing videos (no extraction) | ✅ done |
| 2 | Extraction (dict + LLM) + validation + screening + store mentions | ✅ done |
| 3 | Creator Signals page (read-only) | ✅ done |
| 4 | GitHub Actions workflow (`scripts/scan_creators.py` + `creator-signals.yml`) | ✅ built — ⚠️ **transcripts ARE blocked from the runner (see below)** |
| 5 — Polish | Extraction-retry flag ✅ · multi-creator management (resolve/add/enable-disable + UI) ✅ · email digest & quote-validation ☐ deferred | ✅ mostly done |

### Phase 5 delivered
- **Extraction retry:** new `creator_videos.mentions_extracted_at` flag. Extraction is
  now a separate `_extract_pending` pass over videos with that flag unset, so
  new *and* previously-failed videos are (re)tried each run. A **transient** LLM
  failure (quota / rate-limit) now raises `TransientExtractionError` → the flag
  stays unset → retried next run, instead of silently storing the sparse
  dictionary result as final. (Directly addresses the quota deaths we saw.)
- **Multi-creator management:** `youtube_client.resolve_channel()` (URL/@handle/UC
  id → channel), `creator_signals.add_creator / set_creator_active / list_creators`,
  and a "⚙️ Manage creators" section on the page (add by URL/handle, enable/disable).
- **Email digest ✅:** `engine/mailer.py` (provider-agnostic: **Resend** via
  `RESEND_API_KEY`, or **SMTP** via `SMTP_HOST/USER/PASSWORD`; a no-op if neither
  is set) + `engine/creator_digest.py` (subject + HTML table, carries the same
  "mention ≠ endorsement / not advice" framing). `scan_creators()` emails **only
  when the run turned up new mentions**, so it can't be spammy, and a mail failure
  never breaks the scan. Recipient: `DIGEST_EMAIL_TO`, else the first `OWNER_EMAILS`.
- **Deferred:** quote-based validation (SEC-list validation already covers it well).

### ⚠️ RESOLVED: the datacenter-IP risk was real
First real run (2026-07-10): `done: {'creators': 1, 'new_videos': 15, 'blocked': 15}`.
The **feed works fine** from the runner (all 15 videos found); it's specifically
the **caption endpoint** that rejects GitHub's Azure IP (`IpBlocked`/`RequestBlocked`).
Nothing was stored and everything is queued for retry — the graceful path worked.

Swapping libraries doesn't help — the block is on the IP, not the code
(`youtube-transcript-api` *is* the free OSS project; yt-dlp's own issue tracker
documents the same wall on GitHub Actions). **Solution: fetch server-side.**

`youtube_client.get_transcript()` is now a provider layer, same `(status, text)`
contract:
1. **Supadata** (`SUPADATA_API_KEY`) — hosted API, fetches server-side → works from
   a datacenter IP. **This is the configured path.**
2. **Proxy** (`WEBSHARE_PROXY_*` / `YT_PROXY_URL`) — residential proxy for the direct fetch.
3. **Direct** — youtube-transcript-api; works from a residential IP only.

Supadata notes: `mode=native` (default) = existing captions, **1 credit each**;
`SUPADATA_MODE=auto` adds AI transcription at **2 credits per minute** (~40 for a
20-min video) — don't enable it casually against the 100-credit free tier. Videos
>20 min return HTTP 202 + a jobId which we poll (job checks are free). 206 maps to
`no_captions` (authoritative, no retry); 429 falls through to the direct path and
surfaces as `blocked`, which the scan retries next run.

Volume: ZipTrader posts ~1/day ≈ 30 transcripts/month, inside the free 100.

### To activate Phase 4
1. `git push origin main` (workflow + script + reqs) and `git push space main` (deploys the page + tables).
2. Add GitHub **repo secrets** — reuse the warm-cache ones (`WARM_DATABASE_URL`,
   `FINNHUB_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`) and add two new:
   `GEMINI_API_KEY` (LLM extraction — dictionary fallback without it) and
   `EDGAR_USER_AGENT` (SEC list fetch — has a default UA otherwise).
3. Manually trigger the workflow (Actions → Creator Signals → Run workflow) and
   **watch the log for `transcript_status=blocked`** — that's the datacenter-IP
   risk. If blocked, add a `YT_PROXY_URL` path; if `ok`, the feature is live.

### Phase 4 local validation
A bounded run proved the full chain: transcript (25.6k chars) → **LLM extraction
found the video's 5 bullish picks** (RELY/DV/NOW/META/MBLY, correct stances) →
live screener scores (RELY 68/Buy … MBLY 53/Hold) → stored. The LLM path (quota
permitting) is dramatically better than the deterministic fallback.

## New dependencies (for the cron)
`requirements-signals.txt`: `youtube-transcript-api>=1.0`, `feedparser` (or reuse
`requests`+`beautifulsoup4`), plus the shared DB/screener deps already in
`requirements-warm.txt`. (`youtube-transcript-api` was pip-installed locally
during the Phase 0 spike.)

## Cron secrets/vars
`WARM_DATABASE_URL` (reuse — postgres/bypass), `GEMINI_API_KEY`,
`FINNHUB_API_KEY`, optional `YT_PROXY_URL`.
