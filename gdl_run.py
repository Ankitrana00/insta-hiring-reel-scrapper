#!/usr/bin/env python3
"""In-process gallery-dl runner for Hiring Reels.

Why this exists: `gallery-dl -j` fetches a hashtag's "recent" grid only
(InstagramAPI.tags_sections hardcodes tab="recent") — mostly image posts.
The REELS live on the separate "clips" tab, which the stock runner never
touches. This runner monkey-patches tags_sections to fetch the requested
tabs, then runs one DataJob PER TAG, printing per-tag envelopes separated
by `@@@TAG:<tag>` lines:

  @@@TAG:jobsindelhi
  [ ... the same JSON envelope `gallery-dl -j` would print ... ]
  @@@TAG:jobsinnoida
  [ ... ]

common.parse_gallerydl_batch() splits that back into per-tag results.
ALL tags run in ONE process — interpreter + gallery-dl import cost is paid
once per batch instead of once per tag (~0.5s × N saved), with a small
random sleep between tags so request pacing stays gentle. Nothing is
downloaded — DataJob has no downloader stage at all.

Usage: python gdl_run.py <config.json> <tags_csv> [tabs]
  tags_csv: comma-separated hashtag slugs (lowercase)
  tabs:     comma-separated subset of "recent" and "clips" (default "recent,clips")
"""

from __future__ import annotations

import io
import json
import random
import sys
import time


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: gdl_run.py <config.json> <tags_csv> [tabs]", file=sys.stderr)
        return 2
    config_path = sys.argv[1]
    tags = [t.strip().lower() for t in sys.argv[2].split(",") if t.strip()]
    tabs = [t.strip().lower() for t in (sys.argv[3] if len(sys.argv) > 3 else "recent,clips").split(",") if t.strip()]
    if not tabs:
        tabs = ["recent", "clips"]
    if not tags:
        return 0

    from gallery_dl import config as gdl_config
    from gallery_dl import job
    from gallery_dl.extractor import instagram as ig

    with open(config_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    for key, value in (cfg.get("extractor") or {}).get("instagram", {}).items():
        gdl_config.set(("extractor", "instagram"), key, value)

    def tags_sections_multi(self, tag):
        for tab in tabs:
            data = {
                "include_persistent": "0",
                "max_id": None,
                "page": None,
                "surface": "grid",
                "tab": tab,
            }
            yield from self._pagination_sections(f"/v1/tags/{tag}/sections/", data)

    ig.InstagramAPI.tags_sections = tags_sections_multi

    for index, tag in enumerate(tags):
        if index:
            # keep request pacing gentle between tags — the process-spawn gap
            # this batch replaces used to be the only thing separating them
            time.sleep(random.uniform(1.0, 3.0))
        buffer = io.StringIO()
        try:
            job.DataJob(f"https://www.instagram.com/explore/tags/{tag}/", file=buffer).run()
        except Exception as exc:  # DataJob normally captures its own errors; belt & braces
            buffer.write(json.dumps([[-1, {"error": type(exc).__name__, "message": str(exc)[:300]}]]))
        sys.stdout.write(f"@@@TAG:{tag}\n")
        sys.stdout.write(buffer.getvalue())
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
