# Hiring Reels — local Instagram hiring-reels scraper

> **Standalone tool in its own repo** — deliberately separate from the
> StartupsMap.in app repo, which keeps only the DB side: the `job_reels` migration
> (`ncrstartupmap-nextjs/supabase/migrations/20261105020000_create_job_reels.sql`)
> and its read layer (`src/app/api/reels`, `src/lib/api/reels*`).
>
> No server, no cron, no Vercel spend. Runs on this machine, you review what
> it finds, and only approved reels move on.

Scrapes **hiring reels** from Indian hiring hashtags on Instagram —
**metadata only** (captions, dates, thumbnails; media is never downloaded) —
via [gallery-dl](https://github.com/mikf/gallery-dl), filters to reels, parses
captions for job signals, and produces a review queue. Everything runs on your
machine; a residential IP is also the least ban-prone way to talk to Instagram.

## How it works

```
watchlist.py (40 India hiring hashtags)
        │  daily batch = 10 tags, rotated by day-of-year
        ▼
gdl_run.py ── in-process gallery-dl runner
        │    fetches the hashtag "clips" tab (where REELS live — the stock
        │    gallery-dl runner only walks the "recent" image grid)
        ▼
common.py ── parse → group per post (post_shortcode) → reels-only filter
        │    → caption job-signal parsing (keywords, city, links, emails)
        ▼
out/raw-<date>.ndjson          +  out/review-<date>.html  (visual check)
        │
        ▼  review (you approve/skip each reel)
out/approved-<date>.ndjson
        │
        ▼  push (OPTIONAL — Supabase not configured yet)
public.job_reels (Supabase) → the app's S9 board reads it
```

## Folder map

| File | Role |
| --- | --- |
| `reels.py` | CLI entry — `scrape` / `review` / `push` subcommands |
| `common.py` | gallery-dl output parser, post grouping, caption→job-signals, review-HTML generator |
| `gdl_run.py` | in-process gallery-dl runner — fetches the **clips tab**; prints the same JSON envelope `gallery-dl -j` would |
| `watchlist.py` | the 40 India hiring hashtags for the daily rotation |
| `requirements.txt` | `gallery-dl`, `requests`, `python-dotenv` |
| `.env` | **secrets** — sessionid (+ optional Supabase keys). Never leaves this folder |
| `.env.example` | template documenting every variable |
| `.venv/` | Python virtualenv |
| `out/` | all generated artifacts (raw/approved NDJSON, review HTML, errors log, runtime `config.json`) |

## One-time setup (Windows)

```bash
cd hiring-reels-scraper
py -3 -m venv .venv                              # py launcher; bare `python` may be the Store stub
source .venv/Scripts/activate                    # Git Bash; PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env                           # then fill in IG_SESSIONID (next section)
```

Status: **verified working** — gallery-dl 1.32.19 on Python 3.14, live scrape
returned real hiring reels from this folder on 2026-11-04 and 2026-11-08.

## Getting the `sessionid`

gallery-dl's Instagram extractor needs a logged-in **`sessionid` cookie**
(password login is no longer supported upstream). Use a **throwaway account —
never your main one**:

1. Log in to the throwaway account in your browser.
2. DevTools → **Application** (Storage) → **Cookies** → `https://www.instagram.com`
3. Copy the `sessionid` value into `.env` as `IG_SESSIONID`.

It expires when that account logs out or changes its password — re-extract
then. Anonymous (no cookie) access exists but every tag page hits Instagram's
login wall (verified live).

## Daily workflow

```bash
cd hiring-reels-scraper
.venv/Scripts/python reels.py scrape             # step 1
# step 2: open out/review-<date>.html in a browser and eyeball the finds
.venv/Scripts/python reels.py review out/raw-<date>.ndjson    # step 3
# step 4 (optional, deferred): push — see "Supabase push" below
```

### `scrape` — what it fetches and keeps

| Flag | Meaning |
| --- | --- |
| *(default)* | 10 rotating hashtags/day from `watchlist.py`; **reels only**; `clips` tab only |
| `--tags a,b,c` | specific hashtags (overrides the daily batch) |
| `--all` | whole watchlist (slow — don't do this daily on one cookie) |
| `--max-posts N` | posts per hashtag (default 12; counts all types before the reel filter) |
| `--all-types` | keep image/carousel/video posts too; also fetches the `recent` grid alongside `clips` |
| `--tabs clips,recent` | manual tab override (`top` aborts — upstream parser bug) |
| `--force` | re-scrape tags already done today |

Env knobs in `.env`: `REELS_MAX_POSTS` (12), `REELS_BATCH_SIZE` (10),
`REELS_TAG_TIMEOUT` (180s), `REELS_TABS` (`clips`).

Per-tag output line tells you the yield: `[startupjobs] 4/12 reels kept
(8 non-reel dropped, 4 new)`.

### `review` — the human gate

`[a] approve · [s] skip · [A] approve all rest · [q] quit` — progress is saved
after every answer, so re-running resumes where you stopped. `--auto` approves
every keyword-hit row without prompts (fast, but it will keep meme/advice
posts — the manual pass exists for a reason). Without Supabase configured,
approved rows simply stay local; the tool says so.

### Supabase push — OPTIONAL (currently deferred)

Fill `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` in `.env` and apply the
`job_reels` migration in the app repo first. Then:

```bash
.venv/Scripts/python reels.py push out/approved-<date>.ndjson [--status active|pending]
```

- Idempotent: upserts merge on `shortcode` (`?on_conflict=shortcode` is
  mandatory — the table has two unique constraints).
- `--status pending` feeds a future moderation queue (the column + CHECK
  already exist in the migration).
- The service-role key bypasses RLS and stays in this folder — never commit
  it, never move it server-side; the app reads `job_reels` through its normal
  authenticated client.

## Output files (`out/`)

| File | Contents |
| --- | --- |
| `raw-<date>.ndjson` | every scraped reel that passed the filter (JSON per line) |
| `approved-<date>.ndjson` | rows you approved — the final artifact until push is enabled |
| `review-<date>.html` | offline visual review page (thumbnails are fresh CDN URLs — **they expire within days, review same-day**) |
| `errors-<date>.log` | per-tag failure details, written only when tags fail |
| `config.json` | runtime gallery-dl config — **carries the session cookie**; auto-regenerated every run, delete it if you move machines |

## Row schema (what one scraped reel looks like)

This is the contract the app's UI and `job_reels` table are built against:

```json
{
  "shortcode": "DWvbGDIkhkr",              // unique post id — embed: instagram.com/reel/<shortcode>/embed
  "media_type": "reel",                    // always "reel" (default mode); carousel/post/video with --all-types
  "hashtag_source": "hiringindia",         // watchlist tag it was found under
  "owner_username": "careerswala",         // may be null (private/deleted accounts)
  "owner_fullname": "Shankar | Job | …",   // display name, often keyword-stuffed
  "caption": "🚨 URGENT HIRING …",          // ≤4000 chars, bilingual Hinglish
  "city": "Greater Noida",                 // heuristic: first India city in the caption; often null
  "job_signals": {
    "is_job_post": true,
    "keywords": ["hiring", "vacancy"],     // ≤8 matched job keywords
    "city": "Greater Noida",
    "links": ["https://…"],                // ≤5 URLs in the caption (careers pages, forms)
    "emails": ["hr@company.com"]           // ≤3 — ready-made mailto: CTAs
  },
  "thumbnail_url": "https://…fbcdn.net/…", // signed CDN — EXPIRES within days; review-only
  "posted_at": "2026-04-05 07:07:24",      // approximate; formats vary slightly
  "post_url": "https://www.instagram.com/reel/DWvbGDIkhkr/"
}
```

Key design facts for the UI:

- **Playback is always the embed iframe** (`instagram.com/reel/<shortcode>/embed`)
  — durable, no API needed. `thumbnail_url` is review-time only.
- `city` is a hint, not truth (first-match heuristic).
- Not every keyword-hit is a real job (memes/advice reels happen) — the review
  gate and/or `status: pending` covers that.
- Global tags (`startupjobs`, `hiring`) surface non-India content — skip it in
  review, or curate the watchlist.

## Scheduling (optional)

Windows Task Scheduler, daily:

```
Program:   cmd.exe
Arguments: /c .venv\Scripts\python.exe reels.py scrape
Start in:  C:\path\to\hiring-reels-scraper
```

Review stays manual on purpose — nothing is approved unread.

## Moving / renaming this folder

Safe anytime: every path inside is relative to the folder. After moving, delete
`out/config.json` (it embeds nothing path-dependent, but regenerating is free)
and re-run once — the venv is already self-contained; if Python breaks after a
drive move, recreate it with `py -3 -m venv .venv && pip install -r requirements.txt`.

## Notes & risks

- **ToS / bans:** scraping Instagram violates its ToS and accounts get flagged.
  Mitigations baked in: throwaway account only, metadata-only requests,
  gallery-dl's 6–12s request sleeps, small daily batches, no media downloads.
- **Keep gallery-dl fresh:** Instagram extractors break often —
  `pip install -U gallery-dl` when tags start failing en masse.
- **Old resurfaced reels happen** (a hiring tag can serve a 2021 meme) — check
  `posted_at` in review.
- **Captions are Hinglish** — Devanagari + emoji rendering assumed downstream.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Every tag fails with *"HTTP redirect to login page"* | `IG_SESSIONID` missing/expired — re-extract |
| `challenge_required` / checkpoint errors | Instagram flagged the throwaway account — wait a day, slow down, verify the account once in-browser |
| Empty results for a tag | No recent public reels, or the clips tab returned nothing — try `--all-types` (adds the recent grid) |
| `exit N: ...` per tag after a gallery-dl update | Extractor breakage — pin back or wait for the next release |
| Push says `Upsert failed (400)` | Confirm `on_conflict=shortcode` in `reels.py` and that the migration ran |
| `gallery-dl not installed` | Run from the folder's venv: `.venv/Scripts/python reels.py …`, or `pip install -r requirements.txt` |
