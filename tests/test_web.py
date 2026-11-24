import re

import pytest
from fastapi.testclient import TestClient

from aso_hub import db, metrics, rules, web
from conftest import add_analytics, add_sales_day

LUMEN = "1000000001"


@pytest.fixture
def page(cfg):
    return TestClient(web.app)


def test_pages_render_with_no_data(page):
    for path in ("/", "/apps/lumen-ios", "/apps/lumen-android", "/apps/kettle-ios", "/apps/tandem-ios",
                 "/apps/atlas-ios", "/actions"):
        r = page.get(path)
        assert r.status_code == 200, path
        assert "hasn't been read yet" in r.text


def test_unknown_app_is_404(page):
    assert page.get("/apps/nope").status_code == 404


def test_numbers_and_actions_show(cfg, page):
    conn = db.connect(cfg.database_path)
    for d in metrics.days_back("2026-10-27", 7):
        add_sales_day(conn, d, {(LUMEN, "US", "iPhone", "first_time_units"): 2,
                                (LUMEN, "GB", "iPhone", "first_time_units"): 1})
    add_analytics(conn, LUMEN, metrics.DISCOVERY, "2026-10-27", {
        ("2026-10-26", "US", "iPhone", "impressions"): (500, 400),
        ("2026-10-26", "US", "iPhone", "page_views"): (40, 30)})
    run = db.start_run(conn, "asc")
    db.finish_run(conn, run, True, "ok")
    rules.evaluate(cfg, conn)
    conn.close()

    home = page.get("/").text
    assert "Downloads Oct 27" in home
    assert "1 worth knowing" in home  # low downloads is an FYI, not a to-do
    assert "0 to do" in home
    app_page = page.get("/apps/lumen-ios").text
    assert "Funnel, 7 days to Oct 26" in app_page
    assert "Worth knowing" in app_page
    assert "US downloads are low" in app_page
    # The analytics day (Oct 26) is behind the sales day, so Oct 27 shows dashes.
    assert "–" in app_page


def test_failed_pull_is_shown(cfg, page):
    conn = db.connect(cfg.database_path)
    run = db.start_run(conn, "asc")
    db.finish_run(conn, run, False, "Apple refused the hub's App Store Connect key.")
    conn.close()
    assert "The last App Store Connect pull failed: Apple refused" in page.get("/").text


def test_healthz(page):
    assert page.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize("days, heading", [(7, "Daily"), (30, "Daily"), (90, "Weekly"), (365, "Monthly")])
def test_time_ranges(cfg, page, days, heading):
    conn = db.connect(cfg.database_path)
    add_sales_day(conn, "2026-10-27", {(LUMEN, "US", "iPhone", "first_time_units"): 3})
    add_sales_day(conn, "2026-01-15", {(LUMEN, "US", "iPhone", "first_time_units"): 5})
    add_analytics(conn, LUMEN, metrics.DISCOVERY, "2026-10-28", {
        ("2026-10-15", "US", "iPhone", "impressions"): (100, 90),
        ("2026-10-27", "US", "iPhone", "impressions"): (100, 90)})
    conn.close()
    text = page.get(f"/apps/lumen-ios?days={days}").text
    assert f"<h2>{heading}</h2>" in text
    # Only the 12-month range reaches January's 5 downloads (3 + 5 = 8).
    assert ('<div class="value">8</div>' in text) == (days == 365)
    # Analytics only start Oct 15: inside 7 days back from Oct 27, before the longer ranges.
    assert ("start Oct 15" in text) == (days >= 30)


def test_unknown_range_falls_back_to_7_days(page):
    r = page.get("/?days=12")
    assert r.status_code == 200
    assert '<span aria-current="page">7 days</span>' in r.text


def test_style_and_script_addresses_change_with_their_contents(cfg):
    page = TestClient(web.app)
    css = re.search(r'href="(/static/hub\.css\?v=[0-9a-f]{10})"', page.get("/").text).group(1)
    assert page.get(css).status_code == 200


def test_top_bar_shows_store_icons_only_over_https(cfg, page):
    conn = db.connect(cfg.database_path)
    with conn:
        db.save_snapshot(conn, "2026-10-28", "app", "lumen-ios", {"iconUrl": "https://is1-ssl.mzstatic.com/x.png"})
        db.save_snapshot(conn, "2026-10-28", "app", "kettle-ios", {"iconUrl": "javascript:alert(1)"})
    conn.close()
    text = page.get("/apps/lumen-ios").text
    assert 'src="https://is1-ssl.mzstatic.com/x.png"' in text
    assert "javascript:" not in text
    assert '<a href="/apps/lumen-ios" aria-current="page">' in text
