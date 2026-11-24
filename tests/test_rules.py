import dataclasses

from aso_hub import metrics, rules
from conftest import add_analytics, add_sales_day

LUMEN = "1000000001"
END = "2026-10-27"


def sales_week(conn, per_day: float, territory: str = "US", days: int = 7, end: str = END):
    for d in metrics.days_back(end, days):
        add_sales_day(conn, d, {(LUMEN, territory, "iPhone", "first_time_units"): per_day})


def open_actions(conn):
    return {(r["rule"], r["app_slug"], r["key"]): dict(r)
            for r in conn.execute("SELECT * FROM hub_actions WHERE resolved_at IS NULL")}


def only(cfg, *names):
    """The settings with every rule but `names` turned off."""
    return dataclasses.replace(cfg, rules={n: r | {"enabled": n in names} for n, r in cfg.rules.items()})


def test_low_downloads_fires_below_the_average(cfg, conn):
    sales_week(conn, 2)
    found = rules.findings(only(cfg, "low_downloads"), conn)
    assert [(f.rule, f.app_slug) for f in found] == [("low_downloads", "lumen-ios")]
    assert "averaged 2 first-time US downloads" in found[0].detail
    assert "Oct 27" in found[0].detail


def test_low_downloads_quiet_at_normal_and_ignores_other_countries(cfg, conn):
    sales_week(conn, 4)
    sales_week(conn, 0, territory="GB")
    assert rules.findings(only(cfg, "low_downloads"), conn) == []


def test_low_downloads_only_for_apps_with_a_threshold(cfg, conn):
    # Kettle has no threshold in config.toml, so its low numbers raise nothing.
    for d in metrics.days_back(END, 7):
        add_sales_day(conn, d, {("1000000002", "US", "iPhone", "first_time_units"): 0,
                                (LUMEN, "US", "iPhone", "first_time_units"): 5})
    assert rules.findings(only(cfg, "low_downloads"), conn) == []


def deletions(conn, per_day: float, days: int = 14, end: str = END):
    add_analytics(conn, LUMEN, metrics.INSTALLS, end, {
        (d, "US", "iPhone", "deletions"): (per_day, per_day) for d in metrics.days_back(end, days)})


def test_deletion_rate_fires_above_the_limit(cfg, conn):
    sales_week(conn, 2, days=14)  # 28 downloads
    deletions(conn, 1.5)  # 21 deletions = 75%
    found = rules.findings(only(cfg, "deletion_rate"), conn)
    assert [f.rule for f in found] == ["deletion_rate"]
    assert "75%" in found[0].detail


def test_deletion_rate_needs_enough_downloads(cfg, conn):
    sales_week(conn, 0.5, days=14)  # 7 downloads, under min_downloads
    deletions(conn, 1)
    assert rules.findings(only(cfg, "deletion_rate"), conn) == []


def page_views(conn, baseline: float, latest: float, end: str = END, days: int = 29):
    add_analytics(conn, LUMEN, metrics.DISCOVERY, end, {
        (d, "US", "iPhone", "page_views"): (latest if d == end else baseline,) * 2
        for d in metrics.days_back(end, days)})


def test_page_view_spike_without_downloads(cfg, conn):
    sales_week(conn, 3, days=29)
    page_views(conn, baseline=10, latest=60)
    found = rules.findings(only(cfg, "page_view_spike"), conn)
    assert [f.rule for f in found] == ["page_view_spike"]


def test_page_view_spike_with_downloads_is_fine(cfg, conn):
    sales_week(conn, 3, days=28, end="2026-10-26")
    add_sales_day(conn, END, {(LUMEN, "US", "iPhone", "first_time_units"): 12})
    page_views(conn, baseline=10, latest=60)
    assert rules.findings(only(cfg, "page_view_spike"), conn) == []


def test_small_page_view_bump_is_not_a_spike(cfg, conn):
    sales_week(conn, 3, days=29)
    page_views(conn, baseline=2, latest=15)  # 7x normal but under min_views
    assert rules.findings(only(cfg, "page_view_spike"), conn) == []


def test_actions_open_stay_resolve_and_reopen(cfg, conn, monkeypatch):
    cfg = only(cfg, "low_downloads")
    times = iter(["2026-10-27T13:00:00+00:00", "2026-10-28T13:00:00+00:00",
                  "2026-10-29T13:00:00+00:00", "2026-10-30T13:00:00+00:00"])
    key = ("low_downloads", "lumen-ios", "us")
    sales_week(conn, 2)
    monkeypatch.setattr(rules.db, "now", lambda: next(times))

    rules.evaluate(cfg, conn)
    rules.evaluate(cfg, conn)
    assert open_actions(conn)[key]["first_seen"].startswith("2026-10-27")  # kept, not reset

    conn.execute("UPDATE sales_rows SET value = 5")
    rules.evaluate(cfg, conn)
    assert key not in open_actions(conn)

    conn.execute("UPDATE sales_rows SET value = 1")
    rules.evaluate(cfg, conn)
    assert open_actions(conn)[key]["first_seen"].startswith("2026-10-30")  # a fresh start


def test_turning_a_rule_off_resolves_its_actions(cfg, conn):
    sales_week(conn, 2)
    rules.evaluate(only(cfg, "low_downloads"), conn)
    assert open_actions(conn)
    rules.evaluate(only(cfg), conn)
    assert not open_actions(conn)
