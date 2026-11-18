"""Regression suite for the Hiring Reels scraper — pure-function tests + CLI
flow, all offline (fixtures mirror gallery-dl 1.32.19's real `-j` envelope,
captured live from Instagram on 2026-11-04/08).

Covered layers: envelope parsing (incl. abort surfacing), per-post grouping
and merging, media-type detection (REST + legacy graphql field names), caption
parsing, date handling, reels-only filter, daily rotation math, config
generation, review flow (--auto + resume), and the push column contract.
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

import common
import reels
import watchlist


# ---------------------------------------------------------------------------
# fixtures — shapes verified against live gallery-dl output
# ---------------------------------------------------------------------------

def _rest_envelope() -> str:
    """Real REST-lane envelope: Directory [2, {...}] + Url [3, url, {...}] entries."""
    return json.dumps([
        # reel: Directory (post meta) + video Url + cover Url
        [2, {"post_shortcode": "REEL1", "type": "reel", "count": 1, "date": 1730000000,
             "post_url": "https://www.instagram.com/reel/REEL1/",
             "description": "We are hiring React devs in Gurugram! "
                            "Apply: https://company.com/careers or hr@startup.in",
             "username": "startupjobs", "fullname": "Startup Jobs India"}],
        [3, "https://cdn/vid.mp4", {"post_shortcode": "REEL1", "shortcode": "REEL1vid",
             "type": "reel", "video_url": "https://cdn/vid.mp4",
             "display_url": "https://cdn/video-thumb.jpg"}],
        [3, "https://cdn/cover.jpg", {"post_shortcode": "REEL1", "shortcode": "REEL1cover",
             "type": "reel", "display_url": "https://scontent.cdninstagram.com/cover.jpg"}],
        # carousel: Directory count=2 + two children (per-file shortcodes)
        [2, {"post_shortcode": "CAR2", "type": "post", "count": 2, "date": "2026-11-03T10:00:00Z",
             "post_url": "https://www.instagram.com/p/CAR2/",
             "description": "Walk-in interview tomorrow in Noida and Delhi for freshers",
             "username": "ncr.hiring"}],
        [3, "https://cdn/1.jpg", {"post_shortcode": "CAR2", "shortcode": "CAR2child1",
             "type": "post", "display_url": "https://cdn/1.jpg"}],
        [3, "https://cdn/2.jpg", {"post_shortcode": "CAR2", "shortcode": "CAR2child2",
             "type": "post", "display_url": "https://cdn/2.jpg"}],
        # single image post, NOT a job post
        [2, {"post_shortcode": "P0ST9", "type": "post", "count": 1, "date": 1730000500,
             "post_url": "https://www.instagram.com/p/P0ST9/",
             "description": "sunset vibes at Marine Drive mumbai", "username": "traveler"}],
        [3, "https://cdn/3.jpg", {"post_shortcode": "P0ST9", "shortcode": "P0ST9img",
             "type": "post", "display_url": "https://cdn/3.jpg"}],
    ], indent=2)


ABORT_ENVELOPE = '''[
  [
    -1,
    {
      "error": "AbortExtraction",
      "message": "HTTP redirect to login page (https://www.instagram.com/accounts/login/)"
    }
  ]
]'''


@pytest.fixture()
def rest_rows():
    items, err = common.parse_gallerydl_stdout(_rest_envelope())
    assert err is None
    return common.rows_from_items(items, "hiringindia")


# ---------------------------------------------------------------------------
# envelope parsing
# ---------------------------------------------------------------------------

def test_parse_success_envelope_yields_all_kwdicts():
    items, err = common.parse_gallerydl_stdout(_rest_envelope())
    assert err is None
    assert len(items) == 8  # 3 Directory + 5 Url entries


def test_parse_abort_envelope_becomes_error_with_message():
    items, err = common.parse_gallerydl_stdout(ABORT_ENVELOPE)
    assert items == []
    assert err is not None
    assert "login page" in err and "extraction aborted" in err


def test_parse_empty_and_whitespace_stdout_are_clean():
    assert common.parse_gallerydl_stdout("") == ([], None)
    assert common.parse_gallerydl_stdout("  \n ") == ([], None)


def test_parse_garbage_stdout_is_reported_not_crashed():
    items, err = common.parse_gallerydl_stdout("some banner junk")
    assert items == []
    assert err and "unparseable" in err


def test_parse_rejects_non_array_top_level():
    items, err = common.parse_gallerydl_stdout('{"not": "an array"}')
    assert err and "not a JSON array" in err


# ---------------------------------------------------------------------------
# grouping + merge policy
# ---------------------------------------------------------------------------

def test_rows_group_by_post_shortcode_merging_sidecar_children(rest_rows):
    assert len(rest_rows) == 3
    assert {r["shortcode"] for r in rest_rows} == {"REEL1", "CAR2", "P0ST9"}


def test_row_id_is_post_shortcode_not_per_file_shortcode(rest_rows):
    by_sc = {r["shortcode"]: r for r in rest_rows}
    # the reel's video + cover entries have their own shortcodes; the row id
    # must be the shared post id
    assert "REEL1vid" not in by_sc and "REEL1cover" not in by_sc


def test_caption_read_from_description_field(rest_rows):
    reel = next(r for r in rest_rows if r["shortcode"] == "REEL1")
    assert "hiring React devs" in reel["caption"]


def test_merge_last_nonempty_display_url_wins_cover_thumbnail(rest_rows):
    reel = next(r for r in rest_rows if r["shortcode"] == "REEL1")
    assert reel["thumbnail_url"] == "https://scontent.cdninstagram.com/cover.jpg"


def test_merge_sidecar_children_into_one_row(rest_rows):
    car = next(r for r in rest_rows if r["shortcode"] == "CAR2")
    assert car["media_type"] == "carousel"  # Directory count=2
    assert "Walk-in interview" in car["caption"]


# ---------------------------------------------------------------------------
# media-type detection (REST + legacy graphql field names)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("item,expected", [
    ({"type": "reel"}, "reel"),
    ({"post_url": "https://www.instagram.com/reel/X9/"}, "reel"),
    ({"product_type": "clips"}, "reel"),                     # legacy graphql lane
    ({"type": "igtv", "post_url": "https://www.instagram.com/tv/X/"}, "igtv"),
    ({"type": "video"}, "video"),
    ({"is_video": True}, "video"),
    ({"video_url": "https://v.mp4"}, "video"),
    ({"product_type": "igtv"}, "igtv"),
    ({"count": 2}, "carousel"),
    ({"post_media_count": 3}, "carousel"),
    ({"mediatype": "sidecar"}, "carousel"),
    ({"type": "post", "count": 1}, "post"),
    ({}, "post"),
])
def test_media_type_detection(item, expected):
    assert common._media_type(item) == expected


# ---------------------------------------------------------------------------
# caption → signals
# ---------------------------------------------------------------------------

def test_caption_text_prefers_description_then_falls_back():
    assert common._caption_text({"description": "d-text"}) == "d-text"
    assert common._caption_text({"caption": "c-text"}) == "c-text"
    assert common._caption_text({"caption": {"text": "dict-text"}}) == "dict-text"
    assert common._caption_text({"caption": [{"text": "a"}, {"text": "b"}]}) == "a b"
    assert common._caption_text({"caption_text": "fallback"}) == "fallback"
    assert common._caption_text({}) == ""


def test_posted_at_unix_timestamp_becomes_iso_utc():
    assert common._posted_at({"date": 1730000000}) == "2024-10-27T03:33:20Z"


def test_posted_at_iso_string_passes_through():
    assert common._posted_at({"date": "2026-11-03T10:00:00Z"}) == "2026-11-03T10:00:00Z"


def test_posted_at_string_passthrough_and_missing():
    assert common._posted_at({"date": "2026-11-03 07:07:24"}) == "2026-11-03 07:07:24"
    assert common._posted_at({}) is None
    assert common._posted_at({"date": False}) is None  # bool guard


def test_parse_caption_keywords_city_links_emails():
    sig = common.parse_caption(
        "We are hiring React devs in Gurugram! Apply: https://company.com/careers or hr@startup.in")
    assert sig["is_job_post"] is True
    assert sig["city"] == "Gurugram"
    assert any("company.com" in link for link in sig["links"])
    assert "hr@startup.in" in sig["emails"]


def test_parse_caption_multi_word_city_wins():
    assert common.parse_caption("office in new delhi")["city"] == "New Delhi"
    assert common.parse_caption("plots in greater noida")["city"] == "Greater Noida"


def test_parse_caption_non_job_post():
    sig = common.parse_caption("sunset vibes at Marine Drive mumbai")
    assert sig["is_job_post"] is False
    assert sig["city"] == "Mumbai"  # city detected even without job keywords


def test_parse_caption_empty():
    assert common.parse_caption("")["is_job_post"] is False


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

def test_post_url_prefers_gallerydl_post_url():
    row = {"post_url": "https://www.instagram.com/reel/REEL1/", "shortcode": "REEL1"}
    assert common.post_url_for(row) == "https://www.instagram.com/reel/REEL1/"


def test_post_url_fallback_by_media_type():
    assert common.post_url_for({"shortcode": "X", "media_type": "reel"}).endswith("/reel/X/")
    assert common.post_url_for({"shortcode": "X", "media_type": "post"}).endswith("/p/X/")


# ---------------------------------------------------------------------------
# build_row contract (the push payload shape)
# ---------------------------------------------------------------------------

def test_build_row_full_contract():
    row = common.build_row({
        "post_shortcode": "T1", "type": "reel", "date": 1730000000,
        "post_url": "https://www.instagram.com/reel/T1/",
        "description": "hiring in Gurugram", "username": "u", "fullname": "n",
        "display_url": "https://thumb/1.jpg",
    }, "t")
    assert row["shortcode"] == "T1"
    assert row["media_type"] == "reel"
    assert row["city"] == "Gurugram"  # top-level column, not only inside signals
    assert row["job_signals"]["is_job_post"] is True
    assert row["owner_username"] == "u"
    assert row["post_url"].endswith("/reel/T1/")
    assert row["hashtag_source"] == "t"


def test_build_row_drops_entries_without_shortcode():
    assert common.build_row({"description": "hiring somewhere"}, "t") is None


def test_push_columns_match_contract():
    expected = {"shortcode", "media_type", "hashtag_source", "owner_username",
                "owner_fullname", "caption", "city", "job_signals",
                "thumbnail_url", "posted_at", "status"}
    assert set(reels.PUSH_COLUMNS) == expected
    assert "post_url" not in reels.PUSH_COLUMNS  # extra row key, ignored by push


# ---------------------------------------------------------------------------
# reels-only filter
# ---------------------------------------------------------------------------

def test_filter_media_keeps_reels_only_by_default():
    rows = [{"media_type": "reel"}, {"media_type": "post"}, {"media_type": "carousel"},
            {"media_type": "video"}]
    kept, dropped = reels.filter_media(rows, all_types=False)
    assert [r["media_type"] for r in kept] == ["reel"]
    assert dropped == 3


def test_filter_media_all_types_keeps_everything():
    rows = [{"media_type": "reel"}, {"media_type": "post"}]
    assert reels.filter_media(rows, all_types=True) == (rows, 0)


def test_filter_media_empty_rows():
    assert reels.filter_media([], all_types=False) == ([], 0)


# ---------------------------------------------------------------------------
# daily rotation
# ---------------------------------------------------------------------------

class _FixedNow:
    """Stub for reels.datetime so the rotation day can be controlled."""
    _fake = None

    @staticmethod
    def now():
        return _FixedNow._fake


def test_select_tags_daily_rotation_covers_whole_watchlist(monkeypatch):
    monkeypatch.setattr(reels, "datetime", _FixedNow)
    args = type("Args", (), {"tags": None, "all": False})()
    seen = set()
    for day in range(4):  # 40 tags / batch 10 → 4-day cycle
        _FixedNow._fake = datetime(2026, 1, 1 + day, 12, 0, 0)
        batch = reels.select_tags(args, 10)
        assert len(batch) == 10
        seen.update(batch)
    assert seen == set(watchlist.WATCHLIST)


def test_select_tags_explicit_tags_override_and_normalize():
    args = type("Args", (), {"tags": "#HiringIndia, startupjobs ,", "all": False})()
    assert reels.select_tags(args, 10) == ["hiringindia", "startupjobs"]


def test_select_tags_all_returns_full_watchlist():
    args = type("Args", (), {"tags": None, "all": True})()
    assert reels.select_tags(args, 10) == [t.lower() for t in watchlist.WATCHLIST]


# ---------------------------------------------------------------------------
# gallery-dl config generation
# ---------------------------------------------------------------------------

def test_config_carries_session_cookie_and_caps(tmp_path):
    cfg_path = tmp_path / "config.json"
    common.write_gallerydl_config(cfg_path, "TESTSID", 24)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))["extractor"]["instagram"]
    assert cfg["cookies"] == {"sessionid": "TESTSID"}
    assert cfg["max-posts"] == 24
    assert cfg["videos"] is True  # real boolean — videos=False would hide reels
    assert "sleep-request" in cfg  # the 6-12s throttle stays explicit


def test_config_without_session_id_has_no_cookies(tmp_path):
    cfg_path = tmp_path / "config.json"
    common.write_gallerydl_config(cfg_path, "", 12)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))["extractor"]["instagram"]
    assert "cookies" not in cfg


# ---------------------------------------------------------------------------
# review flow (CLI, offline)
# ---------------------------------------------------------------------------

def _args(file, auto=False):
    return type("Args", (), {"file": str(file), "auto": auto})()


def test_review_auto_keeps_only_job_signal_rows(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setattr(reels, "OUT_DIR", out)
    out.mkdir()
    raw = out / "raw-20261108.ndjson"
    common.append_ndjson(raw, [
        {"shortcode": "R1", "media_type": "reel", "job_signals": {"is_job_post": True}},
        {"shortcode": "N1", "media_type": "post", "job_signals": {"is_job_post": False}},
        {"shortcode": "R2", "media_type": "reel", "job_signals": {"is_job_post": True}},
    ])
    reels.cmd_review(_args(raw, auto=True))
    approved = common.read_ndjson(out / "approved-20261108.ndjson")
    assert {r["shortcode"] for r in approved} == {"R1", "R2"}


def test_review_resume_never_duplicates(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setattr(reels, "OUT_DIR", out)
    out.mkdir()
    raw = out / "raw-20261108.ndjson"
    common.append_ndjson(raw, [{"shortcode": "R1", "job_signals": {"is_job_post": True}}])
    reels.cmd_review(_args(raw, auto=True))
    reels.cmd_review(_args(raw, auto=True))  # rerun must be a no-op
    assert len(common.read_ndjson(out / "approved-20261108.ndjson")) == 1


def test_push_next_hint_depends_on_supabase_config(tmp_path, monkeypatch):
    p = tmp_path / "approved-x.ndjson"
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    assert "stay local" in reels._push_next_hint(p)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    assert "reels.py push" in reels._push_next_hint(p)


# ---------------------------------------------------------------------------
# ndjson io + review html
# ---------------------------------------------------------------------------

def test_ndjson_roundtrip_unicode(tmp_path):
    p = tmp_path / "x.ndjson"
    common.append_ndjson(p, [{"caption": "🚨 हिंदी caption", "n": 1}])
    rows = common.read_ndjson(p)
    assert rows[0]["caption"] == "🚨 हिंदी caption"


def test_review_html_renders_reel_links_and_chips(tmp_path):
    rows = [{"shortcode": "REEL1", "media_type": "reel",
             "posted_at": "2026-04-05T00:00:00Z", "hashtag_source": "t",
             "owner_username": "u", "caption": "hiring <script>x</script>",
             "job_signals": {"keywords": ["hiring"], "city": None},
             "thumbnail_url": "https://thumb/1.jpg", "post_url": None}]
    out = tmp_path / "review.html"
    common.render_review_html(rows, out)
    html_out = out.read_text(encoding="utf-8")
    assert "/reel/REEL1/" in html_out
    assert 'src="https://thumb/1.jpg"' in html_out
    assert "&lt;script&gt;" in html_out  # captions are escaped
    assert "hiring" in html_out
