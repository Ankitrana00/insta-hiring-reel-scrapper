#!/usr/bin/env python3
"""Hiring Reels — local Instagram hashtag scraper -> manual review -> Supabase push.

Subcommands:
  scrape  run gallery-dl metadata-only for today's hashtag batch
          → out/raw-<date>.ndjson + out/review-<date>.html
          (keeps REELS only by default — the S9 goal; --all-types keeps everything)
  review  interactively approve rows from a raw file
          → out/approved-<date>.ndjson
  push    upsert an approved file to the Supabase job_reels table

Nothing is ever downloaded from Instagram (metadata-only `-j` mode); the
session cookie must come from a THROWAWAY account (see README).

Windows note: use the `py -3` launcher — the bare `python` alias may be the
Microsoft Store stub.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounts as accounts_watchlist  # noqa: E402
import common  # noqa: E402
import watchlist  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "out"

# PostgREST `in.()` filters are comma-separated unquoted values — only push
# shortcodes that are provably safe URL/filter tokens.
SHORTCODE_RE = re.compile(r"^[\w-]+$")


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        sys.exit("Missing dependency — run: pip install -r requirements.txt")
    load_dotenv(ROOT / ".env")


def require_gallerydl() -> None:
    if importlib.util.find_spec("gallery_dl") is None:
        sys.exit("gallery-dl not installed — run: pip install -r requirements.txt")


def select_tags(args: argparse.Namespace, batch_size: int) -> list[str]:
    tags = [t.lower() for t in watchlist.WATCHLIST]
    if args.tags:
        return [t.strip().lstrip("#").lower() for t in args.tags.split(",") if t.strip()]
    if args.all:
        return tags
    # Daily rotation: modular per-item indexing stays correct for any
    # (batch, list) size combination — visited once per full cycle.
    day_of_year = datetime.now().timetuple().tm_yday
    start = (day_of_year * batch_size) % len(tags)
    return [tags[(start + i) % len(tags)] for i in range(min(batch_size, len(tags)))]


def filter_media(rows: list[dict], all_types: bool) -> tuple[list[dict], int]:
    """Reels only, by default — the S9 "Hiring Reels" goal is hiring clips,
    so image/carousel/plain-video posts are dropped at parse time instead of
    clogging the review queue. `--all-types` keeps everything."""
    if all_types:
        return rows, 0
    kept = [r for r in rows if r.get("media_type") == "reel"]
    return kept, len(rows) - len(kept)


# ---------------------------------------------------------------------------
# scrape
# ---------------------------------------------------------------------------


def cmd_scrape(args: argparse.Namespace) -> None:
    load_env()
    require_gallerydl()
    session_id = (os.environ.get("IG_SESSIONID") or "").strip()
    max_posts = args.max_posts or int(os.environ.get("REELS_MAX_POSTS", "12"))
    batch_size = int(os.environ.get("REELS_BATCH_SIZE", "15"))
    tag_timeout = float(os.environ.get("REELS_TAG_TIMEOUT", "180"))
    # "clips" is where hashtag reels live; "recent" (grid) is mostly images —
    # only worth fetching when --all-types keeps them.
    tabs_str = args.tabs or os.environ.get("REELS_TABS", "") or ("recent,clips" if args.all_types else "clips")
    tabs = tuple(t.strip().lower() for t in tabs_str.split(",") if t.strip()) or ("clips",)
    try:
        max_age_days = float(args.max_age if args.max_age is not None else os.environ.get("REELS_MAX_AGE_DAYS", "1"))
    except ValueError:
        max_age_days = 1.0

    tags = select_tags(args, batch_size)
    OUT_DIR.mkdir(exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    raw_path = OUT_DIR / f"raw-{today}.ndjson"

    # Cross-day dedupe: a shortcode seen in ANY previous raw/approved file is
    # skipped — the rotation cycle resurfaces tags, and the review queue must
    # never show the same reel twice. (The DB upsert is idempotent on
    # shortcode regardless; this keeps the local artifacts clean.)
    seen_shortcodes: set[str] = set()
    for history in sorted(OUT_DIR.glob("raw-*.ndjson")) + sorted(OUT_DIR.glob("approved-*.ndjson")):
        for row in common.read_ndjson(history):
            if row.get("shortcode"):
                seen_shortcodes.add(str(row["shortcode"]))
    done_tags = {r.get("hashtag_source") for r in common.read_ndjson(raw_path)}

    if not session_id:
        print(
            "WARNING: IG_SESSIONID is not set — running anonymous. Most tag "
            "pages will hit Instagram's login wall and fail.",
            file=sys.stderr,
        )

    config_path = OUT_DIR / "config.json"
    common.write_gallerydl_config(config_path, session_id, max_posts)

    # skip tags already scraped today BEFORE the batch — no wasted requests
    pending_tags: list[str] = []
    skipped = 0
    for tag in tags:
        if tag in done_tags and not args.force:
            print(f"[{tag}] already scraped today — skipped (--force to redo)")
            skipped += 1
        else:
            pending_tags.append(tag)

    results: dict[str, tuple[list[dict], str | None]] = {}
    if pending_tags:
        print(f"Fetching {len(pending_tags)} tag(s) in one runner batch…", flush=True)
        results = common.run_gallerydl_batch(config_path, pending_tags, tag_timeout, tabs=tabs)

    errors: list[str] = []
    ok = 0
    for tag in pending_tags:
        items, err = results.get(tag, ([], "no result — runner did not report this tag"))
        if err:
            errors.append(f"{tag}: {err}")
            print(f"[{tag}] FAILED — {err}", file=sys.stderr, flush=True)
            continue
        reel_rows, dropped = filter_media(common.rows_from_items(items, tag), args.all_types)
        fresh_rows, stale = common.filter_fresh(reel_rows, max_age_days)
        new_rows = [r for r in fresh_rows if r["shortcode"] not in seen_shortcodes]
        dup_count = len(fresh_rows) - len(new_rows)
        common.append_ndjson(raw_path, new_rows)
        seen_shortcodes.update(r["shortcode"] for r in new_rows)
        ok += 1
        kind = "posts" if args.all_types else "reels"
        parts = [f"{stale} older than {max_age_days:g}d", f"{dup_count} seen before", f"{len(new_rows)} new"]
        if not args.all_types:
            parts.insert(0, f"{dropped} non-reel dropped")
        total = len(reel_rows) + dropped
        print(f"[{tag}] {len(fresh_rows)}/{total} {kind} kept ({', '.join(parts)})", flush=True)

    all_rows = common.read_ndjson(raw_path)
    review_path = OUT_DIR / f"review-{today}.html"
    common.render_review_html(all_rows, review_path)

    print(f"\nBatch: {len(tags)} tags — ok {ok}, failed {len(errors)}, skipped {skipped}")
    if errors:
        err_log = OUT_DIR / f"errors-{today}.log"
        err_log.write_text("\n".join(errors) + "\n", encoding="utf-8")
        print(f"Error details: {err_log}")
    print(f"Raw file:    {raw_path}")
    print(f"Review file: {review_path}")
    print(f"Next:        py -3 reels.py review {raw_path.name}")


# ---------------------------------------------------------------------------
# import — browser-captured JSON (the workaround lane when the API lane dies)
# ---------------------------------------------------------------------------


def cmd_import(args: argparse.Namespace) -> None:
    load_env()
    src = Path(args.file)
    if not src.exists():
        sys.exit(f"Not found: {src}")
    raw_text = src.read_text(encoding="utf-8")
    if raw_text.lstrip().startswith("for (;;);"):
        raw_text = raw_text.split(";", 1)[1]  # legacy IG response guard
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        sys.exit(f"{src.name} is not valid JSON ({exc}) — copy the RESPONSE body only, not the headers")

    captured = common.rows_from_browser_capture(payload, args.tag or "browser-capture")
    if not captured:
        sys.exit("No media items found in that capture — copy the response of a request that returns posts")

    OUT_DIR.mkdir(exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    raw_path = OUT_DIR / f"raw-{today}.ndjson"
    seen_shortcodes: set[str] = set()
    for history in sorted(OUT_DIR.glob("raw-*.ndjson")) + sorted(OUT_DIR.glob("approved-*.ndjson")):
        for row in common.read_ndjson(history):
            if row.get("shortcode"):
                seen_shortcodes.add(str(row["shortcode"]))

    try:
        max_age_days = float(args.max_age if args.max_age is not None else os.environ.get("REELS_MAX_AGE_DAYS", "1"))
    except ValueError:
        max_age_days = 1.0

    reel_rows, dropped = filter_media(captured, args.all_types)
    fresh_rows, stale = common.filter_fresh(reel_rows, max_age_days)
    new_rows = [r for r in fresh_rows if r["shortcode"] not in seen_shortcodes]
    dup_count = len(fresh_rows) - len(new_rows)
    common.append_ndjson(raw_path, new_rows)
    seen_shortcodes.update(r["shortcode"] for r in new_rows)

    all_rows = common.read_ndjson(raw_path)
    review_path = OUT_DIR / f"review-{today}.html"
    common.render_review_html(all_rows, review_path)

    kind = "posts" if args.all_types else "reels"
    parts = [f"{stale} older than {max_age_days:g}d", f"{dup_count} seen before", f"{len(new_rows)} new"]
    if not args.all_types:
        parts.insert(0, f"{dropped} non-reel dropped")
    print(f"Captured {len(captured)} media items from {src.name}")
    print(f"→ {len(new_rows)}/{len(captured)} {kind} imported ({', '.join(parts)})")
    if stale and not new_rows:
        print("NOTE: everything in this capture is older than the freshness window.")
        print("For last-day reels: open the tag page → click the 'Recent' tab → scroll →")
        print("copy that response and import it the same way.")
    print(f"Raw file:    {raw_path}")
    print(f"Review file: {review_path}")
    print(f"Next:        py -3 reels.py review {raw_path.name}")


# ---------------------------------------------------------------------------
# accounts — Agent-Reach browser lane (watch hiring accounts' recent posts)
# ---------------------------------------------------------------------------

OPENCLI_FALLBACKS = (
    Path.home() / "AppData" / "Roaming" / "npm" / "opencli",
    Path.home() / ".local" / "bin" / "opencli",
)

URL_SHORTCODE_RE = re.compile(r"instagram\.com/(?:reel|reels|p)/([\w-]+)")


def _find_opencli() -> str:
    found = shutil.which("opencli")
    if found:
        return found
    for candidate in OPENCLI_FALLBACKS:
        if candidate.exists():
            return str(candidate)
    sys.exit("opencli not found — run the Agent Reach browser setup first "
             "(agent-reach setup / agent-reach browser setup)")


EVAL_SCRIPTS_JS = (
    '(() => { const s = [...document.querySelectorAll(' + chr(39) + 'script[type="application/json"]' + chr(39) + ')]; '
    'return JSON.stringify(s.map(x => x.textContent)); })()'
)


def _extract_live_account_rows(opencli_bin: str, account: str, timeout: float,
                               session: str = "ig") -> tuple[list[dict], str | None]:
    """Navigate the bound browser to the profile and pull the hydrated JSON.

    The profile page embeds every post (code, caption.text, taken_at,
    image_versions2, video_versions) in <script type="application/json">
    after hydration — DOM data, not API routes, so Instagram's API removal
    does not affect it. Public profiles render without login; private ones
    need the watch account logged in.
    """
    nav = subprocess.run(
        [opencli_bin, "browser", "open", "--session", session,
         f"https://www.instagram.com/{account}/"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    if nav.returncode != 0:
        return [], f"browser navigation failed: {(nav.stderr or nav.stdout or '')[:200]}"
    proc = subprocess.run(
        [opencli_bin, "browser", "eval", "--session", session, EVAL_SCRIPTS_JS],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    try:
        envelope = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return [], f"unparseable eval output: {(proc.stdout or '')[:200]}"
    if not envelope.get("ok"):
        return [], f"page eval failed: {str(envelope.get('error'))[:200]}"
    try:
        scripts = json.loads(envelope.get("data") or "[]")
    except json.JSONDecodeError:
        return [], "eval returned non-JSON script list"
    rows: list[dict] = []
    for text in scripts:
        try:
            doc = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        rows.extend(common.rows_from_browser_capture(doc, f"@{account}"))
    # dedupe by shortcode (hydration can repeat items across scripts)
    unique: dict[str, dict] = {}
    for row in rows:
        unique.setdefault(row["shortcode"], row)
    return list(unique.values()), None


def _unified_item_to_row(item: dict, account: str) -> dict | None:
    """Map an OpenCLI unified item (unknown-ish fields) to a row."""
    url = str(item.get("url") or item.get("link") or "")
    match = URL_SHORTCODE_RE.search(url)
    shortcode = match.group(1) if match else item.get("id") or item.get("code")
    if not shortcode:
        return None
    caption = next(
        (str(item[k]) for k in ("content", "text", "desc", "description", "body") if item.get(k)),
        "",
    )
    posted = next(
        (item[k] for k in ("created_at", "published_at", "created_utc", "time", "date") if item.get(k)),
        None,
    )
    row = common.browser_item_to_row(
        {"code": shortcode,
         "caption": {"text": caption},
         "taken_at": posted if isinstance(posted, (int, float)) else None,
         "user": {"username": item.get("author") or item.get("username") or account}},
        f"@{account}",
    )
    if row and "/reel" in url:
        row["media_type"] = "reel"
    return row


def _rows_from_opencli_output(stdout: str, account: str) -> tuple[list[dict], str | None]:
    """Parse `opencli instagram user <acc> -f json` output into rows."""
    text = (stdout or "").strip()
    if not text:
        return [], "empty output — is the browser bridge connected? (agent-reach browser setup)"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if re.search(r"(?m)^ok:\s*false", text):
            message = next(
                (line.split("message:", 1)[1].strip() for line in text.splitlines()
                 if line.strip().startswith("message:")),
                text[:200],
            )
            return [], message[:300]
        return [], f"unparseable opencli output: {text[:200]}"
    if isinstance(data, dict) and data.get("ok") is False:
        error = data.get("error") or {}
        return [], str(error.get("message") or "opencli failed")[:300]
    if isinstance(data, dict):
        listed = next((data[k] for k in ("items", "data", "result") if isinstance(data.get(k), list)), None)
        rows_in = listed if listed is not None else []
    else:
        rows_in = data if isinstance(data, list) else []
    rows: list[dict] = []
    for item in rows_in:
        if not isinstance(item, dict):
            continue
        if ("code" in item or "pk" in item) and ("caption" in item or "taken_at" in item or "image_versions2" in item):
            row = common.browser_item_to_row(item, f"@{account}")
        else:
            row = _unified_item_to_row(item, account)
        if row:
            rows.append(row)
    return rows, None


def cmd_accounts(args: argparse.Namespace) -> None:
    """Agent-Reach lane: watch hiring accounts' recent posts via opencli."""
    load_env()
    opencli_bin = _find_opencli()
    accounts = [a.strip().lstrip("@") for a in (args.accounts.split(",") if args.accounts else accounts_watchlist.ACCOUNTS) if a.strip()]

    OUT_DIR.mkdir(exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    raw_path = OUT_DIR / f"raw-{today}.ndjson"
    seen_shortcodes: set[str] = set()
    for history in sorted(OUT_DIR.glob("raw-*.ndjson")) + sorted(OUT_DIR.glob("approved-*.ndjson")):
        for row in common.read_ndjson(history):
            if row.get("shortcode"):
                seen_shortcodes.add(str(row["shortcode"]))
    try:
        max_age_days = float(args.max_age if args.max_age is not None else os.environ.get("REELS_MAX_AGE_DAYS", "1"))
    except ValueError:
        max_age_days = 1.0

    errors: list[str] = []
    ok = 0
    for account in accounts:
        print(f"[@{account}] opening profile and extracting hydrated posts…", flush=True)
        try:
            rows, parse_err = _extract_live_account_rows(opencli_bin, account, args.timeout)
        except subprocess.TimeoutExpired:
            errors.append(f"@{account}: timeout after {args.timeout}s")
            print(f"[@{account}] FAILED — timeout", file=sys.stderr, flush=True)
            continue
        except OSError as exc:
            errors.append(f"@{account}: {exc}")
            print(f"[@{account}] FAILED — {exc}", file=sys.stderr, flush=True)
            continue
        if parse_err and not rows:
            errors.append(f"@{account}: {parse_err}")
            print(f"[@{account}] FAILED — {parse_err}", file=sys.stderr, flush=True)
            continue
        reel_rows, dropped = filter_media(rows, args.all_types)
        fresh_rows, stale = common.filter_fresh(reel_rows, max_age_days)
        new_rows = [r for r in fresh_rows if r["shortcode"] not in seen_shortcodes]
        dup_count = len(fresh_rows) - len(new_rows)
        common.append_ndjson(raw_path, new_rows)
        seen_shortcodes.update(r["shortcode"] for r in new_rows)
        ok += 1
        total = len(reel_rows) + dropped
        kind = "posts" if args.all_types else "reels"
        print(f"[@{account}] {len(fresh_rows)}/{total} {kind} kept ({stale} older than {max_age_days:g}d, "
              f"{dup_count} seen before, {len(new_rows)} new)", flush=True)

    all_rows = common.read_ndjson(raw_path)
    review_path = OUT_DIR / f"review-{today}.html"
    common.render_review_html(all_rows, review_path)

    print(f"\nAccounts: {len(accounts)} — ok {ok}, failed {len(errors)}")
    if errors:
        err_log = OUT_DIR / f"errors-{today}.log"
        err_log.write_text("\n".join(errors) + "\n", encoding="utf-8")
        print(f"Error details: {err_log}")
    print(f"Raw file:    {raw_path}")
    print(f"Review file: {review_path}")
    print(f"Next:        py -3 reels.py review {raw_path.name}")


# ---------------------------------------------------------------------------
# hashtag-browser lane — tag data via the bound browser's network capture
# (Instagram moved tag media from /api/v1/tags/.../sections/ to /api/graphql;
# this lane reads the browser's own graphql responses — drift-resistant)
# ---------------------------------------------------------------------------


def _browser_graphql_bodies(opencli_bin: str, session: str, processed_keys: set) -> dict:
    """Return {key: parsed_body} for NEW graphql responses in the browser capture."""
    out = subprocess.run(
        [opencli_bin, "browser", "network", "--session", session],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    ).stdout
    data = json.loads(out).get("data", {})
    results = {}
    for entry in data.get("entries", []):
        key = str(entry.get("key"))
        if "graphql" not in str(entry.get("url")) or key in processed_keys:
            continue
        detail = subprocess.run(
            [opencli_bin, "browser", "network", "--session", session, "--detail", key],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        ).stdout
        try:
            envelope = json.loads(detail)
            body = envelope.get("data", {})
            results[key] = json.loads(body) if isinstance(body, str) else body
        except (json.JSONDecodeError, ValueError):
            continue
    return results


def cmd_hashtag_browser(args: argparse.Namespace) -> None:
    """Hashtag lane through the bound browser: open tag pages, scroll, and
    harvest the media from the page's own /api/graphql responses."""
    load_env()
    opencli_bin = _find_opencli()
    session = args.session
    tags = [t.strip().lstrip("#") for t in (args.tags.split(",") if args.tags else watchlist.WATCHLIST) if t.strip()]

    OUT_DIR.mkdir(exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    raw_path = OUT_DIR / f"raw-{today}.ndjson"
    seen_shortcodes = set()
    for history in sorted(OUT_DIR.glob("raw-*.ndjson")) + sorted(OUT_DIR.glob("approved-*.ndjson")):
        for row in common.read_ndjson(history):
            if row.get("shortcode"):
                seen_shortcodes.add(str(row["shortcode"]))
    try:
        max_age_days = float(args.max_age if args.max_age is not None else os.environ.get("REELS_MAX_AGE_DAYS", "1"))
    except ValueError:
        max_age_days = 1.0

    errors = []
    ok = 0
    processed_keys = set()
    for tag in tags:
        print(f"[#{tag}] opening tag page in browser...", flush=True)
        try:
            subprocess.run(
                [opencli_bin, "browser", "open", "--session", session,
                 "https://www.instagram.com/explore/tags/" + tag + "/"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=args.timeout,
            )
            for _ in range(args.scrolls):
                time.sleep(2)
                subprocess.run([opencli_bin, "browser", "scroll", "--session", session, "down"],
                               capture_output=True, text=True, timeout=60)
            graph = _browser_graphql_bodies(opencli_bin, session, set())
        except (subprocess.TimeoutExpired, OSError) as exc:
            errors.append("#" + tag + ": " + str(exc))
            print("[" + tag + "] FAILED - " + str(exc), file=sys.stderr, flush=True)
            continue
        rows = []
        for body in graph.values():
            rows.extend(common.rows_from_browser_capture(body, tag))
        if not rows:
            print("[" + tag + "] no media captured (page still loading? increase --scrolls)", flush=True)
            continue
        reel_rows, dropped = filter_media(rows, args.all_types)
        fresh_rows, stale = common.filter_fresh(reel_rows, max_age_days)
        new_rows = [r for r in fresh_rows if r["shortcode"] not in seen_shortcodes]
        dup_count = len(fresh_rows) - len(new_rows)
        common.append_ndjson(raw_path, new_rows)
        seen_shortcodes.update(r["shortcode"] for r in new_rows)
        ok += 1
        kind = "posts" if args.all_types else "reels"
        parts = [str(stale) + " older than " + str(max_age_days) + "d",
                 str(dup_count) + " seen before", str(len(new_rows)) + " new"]
        if not args.all_types:
            parts.insert(0, str(dropped) + " non-reel dropped")
        total = len(reel_rows) + dropped
        print("[" + tag + "] " + str(len(fresh_rows)) + "/" + str(total) + " " + kind
              + " kept (" + ", ".join(parts) + ")", flush=True)

    all_rows = common.read_ndjson(raw_path)
    review_path = OUT_DIR / ("review-" + today + ".html")
    common.render_review_html(all_rows, review_path)

    print("Tags: " + str(len(tags)) + " - ok " + str(ok) + ", failed " + str(len(errors)))
    if errors:
        err_log = OUT_DIR / ("errors-" + today + ".log")
        err_log.write_text("\n".join(errors) + "\n", encoding="utf-8")
        print("Error details: " + str(err_log))
    print("Raw file:    " + str(raw_path))
    print(f"Review file: {review_path}")
    print(f"Next:        py -3 reels.py review {raw_path.name}")


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def _print_row(index: int, total: int, row: dict) -> None:
    signals = row.get("job_signals") or {}
    keywords = ", ".join(signals.get("keywords", [])[:6]) or "—"
    links = signals.get("links") or []
    caption = " ".join(str(row.get("caption") or "").split())[:240] or "(no caption)"
    print(
        f"\n[{index}/{total}] #{row.get('hashtag_source')} · "
        f"@{row.get('owner_username') or '?'} · {(row.get('posted_at') or '')[:10]} · "
        f"{row.get('media_type')} · city={row.get('city') or '—'}"
    )
    print(f"  signals: {keywords}" + (f" | links: {len(links)}" if links else ""))
    print(f"  \u201c{caption}\u201d")
    print(f"  {common.post_url_for(row)}")


def _push_next_hint(approved_path: Path) -> str:
    """Supabase is optional (deferred for now): only point at `push` when it
    is actually configured; otherwise the approved file is the final artifact."""
    if (os.environ.get("SUPABASE_URL") or "").strip() and (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip():
        return f"Next: py -3 reels.py push {approved_path.name}"
    return "Supabase not configured — approved rows stay local (out/approved-*.ndjson)"


def cmd_review(args: argparse.Namespace) -> None:
    load_env()
    src = Path(args.file)
    rows = common.read_ndjson(src)
    if src.name.startswith("raw-"):
        approved_path = OUT_DIR / ("approved-" + src.name[len("raw-"):])
    else:
        approved_path = OUT_DIR / f"approved-{src.stem}.ndjson"
    if not rows:
        sys.exit(f"No rows in {src} — run scrape first")

    already = {r.get("shortcode") for r in common.read_ndjson(approved_path)}
    pending = [r for r in rows if r.get("shortcode") not in already]
    if not pending:
        print(f"All {len(rows)} rows already reviewed -> {approved_path.name}")
        return

    if args.auto:
        keep = [r for r in pending if (r.get("job_signals") or {}).get("is_job_post")]
        common.append_ndjson(approved_path, keep)
        print(f"--auto: approved {len(keep)} of {len(pending)} job-signal rows -> {approved_path.name}")
        print(_push_next_hint(approved_path))
        return

    approve_rest = False
    kept = skipped = 0
    for index, row in enumerate(pending, 1):
        raw_choice = ""
        if not approve_rest:
            _print_row(index, len(pending), row)
            while True:
                try:
                    raw_choice = input(
                        f"approve? [a]approve [s]kip [A]all rest [q]uit > ").strip()
                except EOFError:  # piped or closed stdin — stop cleanly
                    print("\n(input ended — stopping)")
                    raw_choice = "q"
                    break
                if raw_choice in ("A",) or raw_choice.lower() in ("a", "s", "q", ""):
                    break
                print("  answer with a, s, A or q")
            if raw_choice == "A":
                approve_rest = True
            elif raw_choice.lower() == "q":
                print("Stopped early — rerun the same command to resume where you left off.")
                break
        if approve_rest or raw_choice.lower() == "a":
            common.append_ndjson(approved_path, [row])
            kept += 1
        else:
            skipped += 1

    print(f"\nApproved {kept}, skipped {skipped} -> {approved_path}")
    if kept:
        print(f"Next: py -3 reels.py push {approved_path.name}")


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

PUSH_COLUMNS = (
    "shortcode",
    "media_type",
    "hashtag_source",
    "owner_username",
    "owner_fullname",
    "caption",
    "city",
    "job_signals",
    "thumbnail_url",
    "posted_at",
    "status",
)


def cmd_push(args: argparse.Namespace) -> None:
    load_env()
    try:
        import requests
    except ImportError:
        sys.exit("Missing dependency — run: pip install -r requirements.txt")

    base = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not base or not key:
        sys.exit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing — copy .env.example to .env and fill them in.")

    src = Path(args.file)
    rows = [r for r in common.read_ndjson(src) if SHORTCODE_RE.match(str(r.get("shortcode") or ""))]
    if not rows:
        sys.exit(f"No valid rows in {src} (rows without a sane shortcode were dropped)")
    dropped = len(common.read_ndjson(src)) - len(rows)
    if dropped:
        print(f"Dropped {dropped} row(s) with malformed shortcodes", file=sys.stderr)

    payload = [
        {column: row.get(column) for column in PUSH_COLUMNS if column != "status"}
        | {"status": args.status}
        for row in rows
    ]

    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    # on_conflict is REQUIRED: the table has two unique constraints (id PK +
    # shortcode unique) and PostgREST refuses to guess the merge target.
    upsert_headers = {**headers, "Prefer": "resolution=merge-duplicates"}

    new_count = updated_count = 0
    for start in range(0, len(payload), 100):
        chunk = payload[start:start + 100]
        codes = [p["shortcode"] for p in chunk]
        known: set[str] = set()
        try:
            precheck = requests.get(
                f"{base}/rest/v1/job_reels",
                params={"select": "shortcode", "shortcode": f"in.({','.join(codes)})"},
                headers=headers,
                timeout=30,
            )
            if precheck.ok:
                known = {r["shortcode"] for r in precheck.json() if isinstance(r, dict)}
        except requests.RequestException as exc:
            print(f"pre-check failed ({exc}) — counts below may miss merges", file=sys.stderr)
        resp = requests.post(
            f"{base}/rest/v1/job_reels",
            params={"on_conflict": "shortcode"},
            headers=upsert_headers,
            json=chunk,
            timeout=60,
        )
        if resp.status_code not in (200, 201):
            sys.exit(f"Upsert failed ({resp.status_code}): {resp.text[:400]}")
        new_count += sum(1 for c in codes if c not in known)
        updated_count += sum(1 for c in codes if c in known)

    print(f"Pushed {len(payload)} rows -> job_reels ({new_count} new, {updated_count} updated, status={args.status})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reels",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    scrape = sub.add_parser("scrape", help="run gallery-dl metadata-only for today's hashtag batch")
    scrape.add_argument("--tags", help="comma-separated hashtags (overrides the daily batch)")
    scrape.add_argument("--all", action="store_true", help="scrape the whole watchlist (slow)")
    scrape.add_argument("--max-posts", type=int, default=None, help="posts per hashtag (default 12)")
    scrape.add_argument("--all-types", action="store_true",
                        help="also keep image/carousel/video posts (default: reels only)")
    scrape.add_argument("--max-age", type=float, default=None,
                        help="keep only reels posted within the last N days (default 1; "
                             "undated reels are dropped too; REELS_MAX_AGE_DAYS overrides)")
    scrape.add_argument("--tabs", default=None,
                        help='hashtag tabs to fetch: "clips" (reels) and/or "recent" (grid). '
                             'Default: clips, or recent+clips with --all-types')
    scrape.add_argument("--force", action="store_true", help="re-scrape tags already done today")
    scrape.set_defaults(func=cmd_scrape)

    review = sub.add_parser("review", help="interactively approve rows from a raw file")
    review.add_argument("file", help="out/raw-<date>.ndjson")
    review.add_argument("--auto", action="store_true", help="approve every job-signal row without prompting")
    review.set_defaults(func=cmd_review)

    push = sub.add_parser("push", help="upsert an approved file to Supabase job_reels")
    push.add_argument("file", help="out/approved-<date>.ndjson")
    push.add_argument("--status", choices=["active", "pending"], default="active", help="row status (default active)")
    push.set_defaults(func=cmd_push)

    imp = sub.add_parser("import", help="import a browser-captured Instagram JSON file (Network-tab response)")
    imp.add_argument("file", help="path to the captured JSON (any Instagram response shape)")
    imp.add_argument("--tag", default=None, help="label for hashtag_source (default: browser-capture)")
    imp.add_argument("--all-types", action="store_true", help="also keep image/carousel/video posts (default: reels only)")
    imp.add_argument("--max-age", type=float, default=None,
                     help="keep only posts from the last N days (default 1; REELS_MAX_AGE_DAYS overrides)")
    imp.set_defaults(func=cmd_import)

    acc = sub.add_parser("via-accounts", help="Agent-Reach lane: watch hiring accounts' recent posts (opencli)")
    acc.add_argument("--accounts", default=None, help="comma-separated handles (default: accounts.py watchlist)")
    acc.add_argument("--limit", type=int, default=6, help="posts per account (default 6)")
    acc.add_argument("--timeout", type=float, default=120, help="seconds per account (default 120)")
    acc.add_argument("--all-types", action="store_true", help="also keep non-reel posts (default: reels only)")
    acc.add_argument("--max-age", type=float, default=None, help="keep only posts from the last N days (default 1)")
    acc.set_defaults(func=cmd_accounts)

    hb = sub.add_parser("hashtag-browser", help="hashtag lane via the bound browser (network capture) - works while the API lane is dead")
    hb.add_argument("--tags", default=None, help="comma-separated hashtags (default: full watchlist)")
    hb.add_argument("--scrolls", type=int, default=4, help="scroll passes per tag to load more reels (default 4)")
    hb.add_argument("--session", default="ig", help="bound browser session name (default ig)")
    hb.add_argument("--timeout", type=float, default=120, help="seconds per page operation (default 120)")
    hb.add_argument("--all-types", action="store_true", help="also keep non-reel posts (default: reels only)")
    hb.add_argument("--max-age", type=float, default=None, help="keep only posts from the last N days (default 1)")
    hb.set_defaults(func=cmd_hashtag_browser)
    return parser


def main() -> None:
    # Windows consoles default to cp1252 and crash on emoji/captions.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
