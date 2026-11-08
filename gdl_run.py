#!/usr/bin/env python3
"""In-process gallery-dl runner for Hiring Reels.

Why this exists: `gallery-dl -j` fetches a hashtag's "recent" grid only
(InstagramAPI.tags_sections hardcodes tab="recent") — mostly image posts.
The REELS live on the separate "clips" tab, which the stock runner never
touches. This runner monkey-patches tags_sections to fetch the requested
tabs, then runs a DataJob and prints the SAME JSON envelope `gallery-dl -j`
would print, so common.parse_gallerydl_stdout keeps working unchanged.

Usage: python gdl_run.py <config.json> <url> [tabs]
  tabs: comma-separated subset of "recent" and "clips" (default "recent,clips")

Nothing is downloaded — DataJob has no downloader stage at all.
"""

from __future__ import annotations

import io
import json
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: gdl_run.py <config.json> <url> [tabs]", file=sys.stderr)
        return 2
    config_path, url = sys.argv[1], sys.argv[2]
    tabs = [t.strip().lower() for t in (sys.argv[3] if len(sys.argv) > 3 else "recent,clips").split(",") if t.strip()]
    if not tabs:
        tabs = ["recent", "clips"]

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

    buffer = io.StringIO()
    job.DataJob(url, file=buffer).run()
    sys.stdout.write(buffer.getvalue())
    return 0


if __name__ == "__main__":
    sys.exit(main())
