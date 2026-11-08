"""Shared helpers for the Hiring Reels local scraper.

Pipeline: `gallery-dl -j` (--dump-json) never downloads — it dumps ONE JSON
array of message entries (Directory `[2, {post-meta}]`, Url `[3, url,
{file-meta}]`, abort `[-1, {error, message}]`, exit code 0 either way).
parse_gallerydl_stdout extracts the metadata dicts, rows_from_items groups
them into one row per post, the caption is parsed for job signals, and the
rows land in raw NDJSON on disk. The generated gallery-dl config exists only
to carry the throwaway-account `sessionid` cookie and the per-tag post cap.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Caption parsing
# ---------------------------------------------------------------------------

# Substring matching is deliberate: "job" also matches "jobalert" and "jobs".
# The list reads as signals, not exact tokens.
JOB_KEYWORDS: list[str] = [
    "hiring",
    "we are hiring",
    "we're hiring",
    "now hiring",
    "hiringnow",
    "job",
    "jobalert",
    "jobopening",
    "jobsearch",
    "vacancy",
    "vacancies",
    "opening",
    "openings",
    "position",
    "opportunity",
    "walk-in",
    "walkin",
    "walk in interview",
    "internship",
    "intern",
    "fresher",
    "freshers",
    "apply",
    "applynow",
    "recruitment",
    "recruiting",
    "join our team",
    "join us",
    "careers",
    "career opportunity",
    "work from home",
    "wfh",
    "remote job",
]

# Longest / most-specific names first so "new delhi" wins over "delhi" and
# "greater noida" over "noida". Matched with word boundaries.
INDIA_CITIES: list[str] = [
    "new delhi",
    "greater noida",
    "delhi",
    "ncr",
    "gurugram",
    "gurgaon",
    "noida",
    "faridabad",
    "ghaziabad",
    "mumbai",
    "navi mumbai",
    "bangalore",
    "bengaluru",
    "hyderabad",
    "secunderabad",
    "pune",
    "chennai",
    "kolkata",
    "ahmedabad",
    "jaipur",
    "chandigarh",
    "mohali",
    "indore",
    "bhopal",
    "nagpur",
    "surat",
    "vadodara",
    "lucknow",
    "kanpur",
    "kochi",
    "coimbatore",
    "bhubaneswar",
    "thiruvananthapuram",
    "trivandrum",
    "dehradun",
    "guwahati",
    "patna",
    "ranchi",
    "raipur",
    "jabalpur",
    "visakhapatnam",
    "mysore",
]

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def parse_caption(caption: str | None) -> dict:
    """Extract job-post signals from a raw Instagram caption."""
    text = (caption or "").lower()
    keywords: list[str] = []
    for keyword in JOB_KEYWORDS:
        if keyword in text and keyword not in keywords:
            keywords.append(keyword)
    city = None
    for candidate in INDIA_CITIES:
        if re.search(rf"\b{re.escape(candidate)}\b", text):
            city = candidate.title()
            break
    return {
        "is_job_post": bool(keywords),
        "keywords": keywords[:8],
        "city": city,
        "links": sorted(set(URL_RE.findall(caption or "")))[:5],
        "emails": sorted(set(EMAIL_RE.findall(caption or "")))[:3],
    }


# ---------------------------------------------------------------------------
# gallery-dl output → row mapping (defensive: IG metadata keys drift)
# ---------------------------------------------------------------------------


def _caption_text(item: dict) -> str:
    # REST lane (verified live): the caption text is `description`.
    # graphql-lane shapes keep the `caption` / `caption_text` fallbacks.
    caption = item.get("description") or item.get("caption")
    if isinstance(caption, dict):  # some API shapes: {"text": "..."}
        caption = caption.get("text")
    if isinstance(caption, list):  # edge-list shape: [{"node": {"text": ...}}]
        parts = []
        for part in caption:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(part))
        caption = " ".join(parts)
    if not isinstance(caption, str):
        caption = item.get("caption_text") or ""
    return caption.strip()


def _media_type(item: dict) -> str:
    # REST lane: gallery-dl computes `type` ("reel"/"post") and `post_url`
    # (/reel/… vs /p/…) itself — trust those first (verified live).
    gdl_type = str(item.get("type") or "").lower()
    post_url = str(item.get("post_url") or "")
    if gdl_type == "reel" or "/reel/" in post_url:
        return "reel"
    if gdl_type == "igtv" or "/tv/" in post_url:
        return "igtv"
    if gdl_type == "video":
        return "video"
    # graphql-lane fallbacks (field names from older API shapes)
    product = str(item.get("product_type") or "").lower()
    if product == "clips":
        return "reel"
    if product == "igtv":
        return "igtv"
    mediatype = str(item.get("post_mediatype") or item.get("mediatype") or "").lower()
    if "video" in mediatype or item.get("video_url") or item.get("is_video") is True:
        return "video"
    if "sidecar" in mediatype or "carousel" in mediatype:
        return "carousel"
    if "album" in gdl_type:
        return "carousel"
    children = item.get("children")
    if isinstance(children, list) and len(children) > 1:
        return "carousel"
    # REST lane: Directory entries carry `count` = number of files in the post
    count = item.get("count")
    if isinstance(count, int) and count > 1:
        return "carousel"
    for count_key in ("post_media_count", "media_count"):
        count = item.get(count_key)
        if isinstance(count, int) and count > 1:
            return "carousel"
    return "post"


def _posted_at(item: dict) -> str | None:
    ts = item.get("date") or item.get("timestamp")
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)) and ts > 0:
        # gallery-dl's instagram `date` is a unix timestamp (seconds).
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(ts, str) and ts:
        return ts  # some lanes already emit an ISO string
    return None


def build_row(item: dict, hashtag: str) -> dict | None:
    """One gallery-dl entry → one job_reels-shaped row (or None).

    The post-level id is `post_shortcode` (present on Directory AND file
    entries; a sidecar's per-file `shortcode` identifies the child media, not
    the post). `post_url` is gallery-dl's prebuilt /reel/… or /p/… link.
    """
    shortcode = item.get("post_shortcode") or item.get("shortcode")
    if not shortcode:
        return None
    caption = _caption_text(item)
    signals = parse_caption(caption)
    return {
        "shortcode": str(shortcode),
        "media_type": _media_type(item),
        "hashtag_source": hashtag,
        "owner_username": item.get("owner_username") or item.get("username"),
        "owner_fullname": item.get("fullname") or item.get("full_name"),
        "caption": caption[:4000] or None,
        "city": signals.get("city"),
        "job_signals": signals,
        "thumbnail_url": item.get("display_url") or item.get("thumbnail_url"),
        "posted_at": _posted_at(item),
        "post_url": item.get("post_url"),
    }


def rows_from_items(items: list[dict], hashtag: str) -> list[dict]:
    """Group gallery-dl's message entries into one row per post.

    Each post arrives as a Directory entry (post-level metadata, incl. the
    `description` caption and `count`) followed by one Url entry per file
    (adds display_url/video_url). All entries of a post share
    `post_shortcode` — that is the grouping key. Merge policy: the first
    entry supplies the base, later entries fill missing fields, and the LAST
    non-empty display_url wins (the reel-cover entry is the best thumbnail
    and comes after the video entry).
    """
    merged: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        shortcode = item.get("post_shortcode") or item.get("shortcode")
        if not shortcode:
            continue
        shortcode = str(shortcode)
        base = merged.get(shortcode)
        if base is None:
            merged[shortcode] = dict(item)
            continue
        for key, value in item.items():
            if key == "description":
                if not base.get("description"):
                    base["description"] = value
            elif key == "display_url":
                if value:
                    base["display_url"] = value
            elif base.get(key) in (None, ""):
                base[key] = value
        if item.get("video_url"):
            base.setdefault("video_url", item["video_url"])

    rows: list[dict] = []
    for item in merged.values():
        row = build_row(item, hashtag)
        if row:
            rows.append(row)
    return rows


def post_url_for(row: dict) -> str:
    # gallery-dl prebuilt /reel/… or /p/… link when present (REST lane),
    # shortcode-based construction otherwise.
    url = str(row.get("post_url") or "")
    if url.startswith("https://www.instagram.com/"):
        return url
    shortcode = row.get("shortcode") or ""
    if row.get("media_type") in ("reel", "igtv"):
        return f"https://www.instagram.com/reel/{shortcode}/"
    return f"https://www.instagram.com/p/{shortcode}/"


# ---------------------------------------------------------------------------
# gallery-dl invocation
# ---------------------------------------------------------------------------


def write_gallerydl_config(path: Path, session_id: str, max_posts: int) -> None:
    """Runtime config passed with `--config` (NOT `-o` argv pairs — those
    parse values as JSON, so `False` stays a truthy string, and cookie dicts
    are unquotable on Windows)."""
    instagram: dict = {
        "max-posts": max_posts,
        # gallery-dl's instagram lane already throttles 6-12s between
        # requests; keep it explicit so a future edit sees the dial.
        "sleep-request": "6.0-12.0",
        # Metadata dump only — nothing is ever downloaded (`-j` runs a
        # DataJob, which has no downloader stage at all). Videos stay enabled
        # ON PURPOSE: with videos=False the extractor skips yielding video
        # file entries entirely, so reel metadata (product_type, video_url,
        # the cover thumbnail) would be missing from the dump.
        "videos": True,
    }
    if session_id:
        instagram["cookies"] = {"sessionid": session_id}
    path.write_text(
        json.dumps({"extractor": {"instagram": instagram}}, indent=2),
        encoding="utf-8",
    )


GDL_RUNNER = Path(__file__).resolve().parent / "gdl_run.py"


def run_gallerydl_tag(config_path: Path, tag: str, timeout: float,
                      tabs: tuple[str, ...] = ("clips",)) -> tuple[list[dict], str | None]:
    """Run the in-process runner for one hashtag page. Returns (items, error).

    Default tab is "clips" — that is where hashtag REELS live; the stock
    `gallery-dl -j` runner only fetches the "recent" grid, which our live test
    showed yields ~0 reels on hiring tags. Pass "recent,clips" with --all-types.
    """
    url = f"https://www.instagram.com/explore/tags/{tag}/"
    cmd = [sys.executable, str(GDL_RUNNER), str(config_path), url, ",".join(tabs)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [], f"timeout after {timeout:.0f}s"
    except OSError as exc:
        return [], f"failed to launch gallery-dl: {exc}"
    if proc.returncode != 0:
        stderr_lines = [line for line in (proc.stderr or "").strip().splitlines() if line.strip()]
        last = stderr_lines[-1] if stderr_lines else "(no stderr)"
        return [], f"exit {proc.returncode}: {last[:300]}"
    return parse_gallerydl_stdout(proc.stdout or "")


def parse_gallerydl_stdout(stdout: str) -> tuple[list[dict], str | None]:
    """Parse `gallery-dl -j` output into metadata dicts.

    `-j` is --dump-json: a DataJob runs (never downloads) and prints ONE JSON
    array of message tuples — [2, {post-meta}] Directory, [3, url, {file-meta}]
    Url, and on abort [-1, {"error", "message"}] — while still exiting 0. So
    the whole stdout must be parsed as a single document, not line-by-line.
    """
    text = stdout.strip()
    if not text:
        return [], None
    try:
        entries = json.loads(text)
    except json.JSONDecodeError:
        return [], f"unparseable gallery-dl output: {text[:200]}"
    if not isinstance(entries, list):
        return [], "unexpected gallery-dl output shape (not a JSON array)"
    items: list[dict] = []
    for entry in entries:
        if not isinstance(entry, list) or not entry or not isinstance(entry[-1], dict):
            continue
        payload = entry[-1]
        if entry[0] == -1 and isinstance(payload.get("error"), str):
            # Aborted extraction (login wall, checkpoint, ...) — exit code is
            # still 0, so this entry is the only failure signal.
            detail = payload.get("message") or payload.get("error") or "unknown error"
            return [], f"extraction aborted: {str(detail)[:300]}"
        items.append(payload)
    return items, None


# ---------------------------------------------------------------------------
# NDJSON files
# ---------------------------------------------------------------------------


def read_ndjson(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_ndjson(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Offline review HTML (thumbnails are fresh CDN URLs at scrape time; they
# expire, so this file is only good for the day it was generated)
# ---------------------------------------------------------------------------

_REVIEW_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hiring Reels review — {date}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ background: #101014; color: #e8e8ea; font: 14px/1.5 system-ui, sans-serif; margin: 0; padding: 24px; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .sub {{ color: #9a9aa2; margin-bottom: 20px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }}
  .card {{ background: #191920; border: 1px solid #2b2b34; border-radius: 12px; padding: 12px; }}
  .meta {{ color: #9a9aa2; font-size: 12px; margin-bottom: 8px; }}
  .body {{ display: flex; gap: 10px; }}
  .body img {{ width: 120px; height: 120px; object-fit: cover; border-radius: 8px; flex: none; background: #22222a; }}
  .noimg {{ width: 120px; height: 120px; flex: none; display: grid; place-items: center; color: #55555f;
           border-radius: 8px; background: #22222a; font-size: 11px; }}
  .cap {{ max-height: 120px; overflow: auto; white-space: pre-wrap; word-break: break-word; font-size: 13px; }}
  .chips {{ margin: 10px 0 8px; display: flex; flex-wrap: wrap; gap: 4px; }}
  .chip {{ background: #2b1f3a; color: #d0a6ff; border-radius: 999px; padding: 1px 8px; font-size: 11px; }}
  .chip.dim {{ background: #22222a; color: #77777f; }}
  a {{ color: #8ab4ff; font-size: 12px; }}
</style>
</head>
<body>
<h1>Hiring Reels — scrape review</h1>
<p class="sub">{date} · {count} posts · thumbnails expire with the CDN URLs, review today</p>
<div class="grid">{cards}
</div>
</body>
</html>
"""


def render_review_html(rows: list[dict], path: Path) -> None:
    cards: list[str] = []
    for index, row in enumerate(rows, 1):
        signals = row.get("job_signals") or {}
        chips = "".join(f'<span class="chip">{html.escape(k)}</span>' for k in signals.get("keywords", []))
        if signals.get("city") and row.get("city"):
            chips += f'<span class="chip">{html.escape(str(row["city"]))}</span>'
        if signals.get("emails"):
            chips += f'<span class="chip">{html.escape(len(signals["emails"]).__str__() + " email(s)")}</span>'
        if not chips:
            chips = '<span class="chip dim">no job keywords</span>'
        thumb = row.get("thumbnail_url") or ""
        media = html.escape(str(row.get("media_type") or "post"))
        media_html = f' · {media}' if media not in ("post", "") else ""
        body_img = (
            f'<img src="{html.escape(thumb)}" loading="lazy" referrerpolicy="no-referrer" alt="">'
            if thumb
            else '<div class="noimg">expired /<br>no thumbnail</div>'
        )
        caption = html.escape(str(row.get("caption") or "(no caption)"))
        cards.append(f"""
  <div class="card">
    <div class="meta">#{index} · {html.escape(str(row.get("posted_at") or "")[:10] or "?")} · #{html.escape(str(row.get("hashtag_source") or ""))}{media_html} · @{html.escape(str(row.get("owner_username") or "unknown"))}</div>
    <div class="body">{body_img}<div class="cap">{caption}</div></div>
    <div class="chips">{chips}</div>
    <a href="{html.escape(post_url_for(row))}" target="_blank" rel="noopener">open on Instagram ↗</a>
  </div>""")
    path.write_text(
        _REVIEW_TEMPLATE.format(
            date=datetime.now().strftime("%Y-%m-%d %H:%M"),
            count=len(rows),
            cards="".join(cards),
        ),
        encoding="utf-8",
    )
