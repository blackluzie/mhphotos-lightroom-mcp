"""0.1.29: photo statistics page (photo_stats)."""

import re
from datetime import date, datetime
from pathlib import Path

import pytest
from mhphotos_lightroom_mcp import photo_stats as ps
from mhphotos_lightroom_mcp import server

from tests.test_catalog_insights import row, save_snapshot

pytestmark = pytest.mark.anyio


def photos():
    out = []
    for i in range(30):
        out.append(row(f"a{i}", f"2026-06-{i % 28 + 1:02d}T19:{i % 60:02d}:00", cam="ILCE-7M3", lens="FE 85mm F1.4 GM",
                       fl=85, ap=2.0, iso=100, ss=0.004, r=4 if i % 3 == 0 else None, w=6000, h=4000, size=1024 ** 2 * 50))
    for i in range(25):
        out.append(row(f"b{i}", f"2025-07-{i % 28 + 1:02d}T13:00:00", cam="ILCE-7M3", lens="FE 24-70mm F2.8 GM",
                       fl=70, ap=8.0, iso=400, ss=0.001, edited=1, w=4000, h=6000, size=1024 ** 2 * 40))
    out.append(row("old", "2019-01-01T10:00:00", cam="Canon EOS 5D", lens="EF 50mm f/1.8", fl=50, size=1024 ** 2))
    out.append(row("vid", "2026-06-01T10:00:00", fmt="VIDEO"))
    return out


def test_compute_counts_and_hit_rate():
    s = ps.compute(photos(), today=date(2026, 11, 11))
    assert s["photos"] == 56  # video left out
    assert s["years_available"] == [2019, 2025, 2026]
    assert s["hit_rate"]["count"] == 10 and s["keepers"]["count"] == 35
    assert s["focal"][0][0] in (85, 70)
    assert [r["lens"] for r in s["lenses_unused"]] == ["EF 50mm f/1.8", "FE 24-70mm F2.8 GM"]
    assert s["format"]["landscape"] > 0 and s["format"]["portrait"] > 0
    only = ps.compute(photos(), [2026], today=date(2026, 11, 11))
    assert only["photos"] == 30 and only["strongest_month"] == 6 and only["empty_months"][0] == 1


def test_focal_finder_and_open_aperture():
    s = ps.compute(photos(), today=date(2026, 11, 11))
    assert s["focal_finder"]["suggestion"] == 70 and s["focal_finder"]["photos_covered"] == 25
    rows = {r["lens"]: r for r in s["open_aperture"]}
    assert rows["FE 85mm F1.4 GM"]["widest"] == 1.4 and rows["FE 85mm F1.4 GM"]["at_widest_share"] == 0.0
    assert ps.max_aperture("FE 24-70mm F2.8 GM") == 2.8 and ps.max_aperture("EF 50mm f/1.8") == 1.8
    assert ps.max_aperture("some lens") is None


def test_light_of_golden_hour_and_midday():
    p = {"lat": 51.0, "lon": 10.0}
    assert ps.light_of(p, datetime(2026, 6, 21, 13, 0)) == "day"
    assert ps.light_of(p, datetime(2026, 6, 21, 21, 15)) == "golden"
    assert ps.light_of(p, datetime(2026, 6, 21, 1, 0)) == "night"
    assert ps.light_of(p, datetime(2026, 12, 21, 16, 0)) == "golden"


def test_old_snapshot_without_exposure_fields_still_works():
    s = ps.compute([row("x", "2024-05-01T10:00:00", cam="A", lens="L", fl=35)], today=date(2026, 11, 11))
    assert s["exposure_known"] == {"aperture": 0, "iso": 0, "shutter": 0} and s["aperture"] == []


def test_html_is_self_contained_and_escapes():
    page = ps.render_html(ps.compute_all(photos(), date(2026, 11, 11)), "<b>x</b>")
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "</script><script" not in page
    assert not re.search(r'(src|href)="https?://', page)


async def test_tool_writes_page_and_reports_numbers(tmp_path):
    save_snapshot(photos())
    res = await server.photo_stats(2026)
    assert res["stats"]["photos"] == 30 and "heatmap" not in res["stats"]
    page = Path(res["page"]).read_text(encoding="utf-8")
    assert "by_year_stats" in page and "const ALL=" in page
    assert (await server.photo_stats(year=10))["error"]


async def test_tool_without_snapshot_explains():
    assert (await server.photo_stats()).get("code") == "no_snapshot"
