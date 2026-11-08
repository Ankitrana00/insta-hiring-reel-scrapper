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
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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
    batch_size = int(os.environ.get("REELS_BATCH_SIZE", "10"))
    tag_timeout = float(os.environ.get("REELS_TAG_TIMEOUT", "180"))
    # "clips" is where hashtag reels live; "recent" (grid) is mostly images —
    # only worth fetching when --all-types keeps them.
    tabs_str = args.tabs or os.environ.get("REELS_TABS", "") or ("recent,clips" if args.all_types else "clips")
    tabs = tuple(t.strip().lower() for t in tabs_str.split(",") if t.strip()) or ("clips",)

    tags = select_tags(args, batch_size)
    OUT_DIR.mkdir(exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    raw_path = OUT_DIR / f"raw-{today}.ndjson"

    existing_rows = common.read_ndjson(raw_path)
    seen_shortcodes = {r.get("shortcode") for r in existing_rows}
    done_tags = {r.get("hashtag_source") for r in existing_rows}

    if not session_id:
        print(
            "WARNING: IG_SESSIONID is not set — running anonymous. Most tag "
            "pages will hit Instagram's login wall and fail.",
            file=sys.stderr,
        )

    config_path = OUT_DIR / "config.json"
    common.write_gallerydl_config(config_path, session_id, max_posts)

    errors: list[str] = []
    ok = skipped = 0
    for tag in tags:
        if tag in done_tags and not args.force:
            print(f"[{tag}] already scraped today — skipped (--force to redo)")
            skipped += 1
            continue
        print(f"[{tag}] fetching metadata (max {max_posts} posts)…", flush=True)
        items, err = common.run_gallerydl_tag(config_path, tag, tag_timeout, tabs=tabs)
        if err:
            errors.append(f"{tag}: {err}")
            print(f"[{tag}] FAILED — {err}", file=sys.stderr, flush=True)
            continue
        kept_rows, dropped = filter_media(common.rows_from_items(items, tag), args.all_types)
        fresh = [r for r in kept_rows if r["shortcode"] not in seen_shortcodes]
        common.append_ndjson(raw_path, fresh)
        seen_shortcodes.update(r["shortcode"] for r in fresh)
        ok += 1
        if args.all_types:
            print(f"[{tag}] {len(kept_rows)} posts ({len(fresh)} new)", flush=True)
        else:
            total = len(kept_rows) + dropped
            print(f"[{tag}] {len(kept_rows)}/{total} reels kept ({dropped} non-reel dropped, {len(fresh)} new)", flush=True)

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
