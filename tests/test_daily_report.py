from aso_hub import daily_report, db
from conftest import add_sales_day

LUMEN = "1000000001"


def keyword(text, position, source="MANUAL"):
    return {"keywordId": text, "text": text, "source": source, "latestPosition": position}


def test_snapshot_has_the_shape_the_discord_report_reads(cfg, conn):
    # Apple leaves out days with nothing sold: Nov 11 is missing and counts as 0.
    add_sales_day(conn, "2026-11-10", {(LUMEN, "US", "iPhone", "first_time_units"): 5})
    add_sales_day(conn, "2026-11-12", {(LUMEN, "US", "iPhone", "first_time_units"): 8})
    with conn:
        db.save_snapshot(conn, "2026-11-12", "keywords", "lumen-ios:us",
                         [keyword("offline audiobook", 41), keyword("player", None, "TITLE"),
                          keyword("bookorbit", 2, "SUBTITLE")])

    snap = daily_report.snapshot(conn, cfg)

    assert snap["source"] == "hub"
    lumen = next(a for a in snap["apps"] if a["id"] == "lumen-ios")
    assert lumen["name"] == "Lumen" and lumen["platform"] == "ios"
    # Starts at Lumen's first day with numbers, not a full week of zeros.
    assert lumen["downloads"] == [{"date": "2026-11-10", "value": 5}, {"date": "2026-11-11", "value": 0},
                                   {"date": "2026-11-12", "value": 8}]
    assert all(type(p["value"]) is int for p in lumen["downloads"])
    assert [p["value"] for p in lumen["proceeds"]] == [0, 0, 0]
    # Single words from the listing stay hidden, as on the hub's pages; kept names show.
    assert lumen["ranks"] == [{"term": "offline audiobook", "country": "us", "position": 41},
                               {"term": "bookorbit", "country": "us", "position": 2}]
    android = next(a for a in snap["apps"] if a["id"] == "lumen-android")
    assert android["platform"] == "android" and android["downloads"] == [] and android["proceeds"] == []


def test_an_app_with_no_numbers_yet_has_empty_series(cfg, conn):
    add_sales_day(conn, "2026-11-12", {(LUMEN, "US", "iPhone", "first_time_units"): 8})
    snap = daily_report.snapshot(conn, cfg)
    atlas = next(a for a in snap["apps"] if a["id"] == "atlas-ios")
    assert atlas["downloads"] == [] and atlas["ranks"] == []


def test_visibility_and_audit_come_per_app_and_per_linked_group(cfg, conn):
    add_sales_day(conn, "2026-11-12", {(LUMEN, "US", "iPhone", "first_time_units"): 8})
    with conn:
        db.save_snapshot(conn, "2026-11-23", "ids", "all", {"lumen-ios": "a1", "lumen-android": "a2",
                                                            "tandem-ios": "a3"})
        db.save_snapshot(conn, "2026-11-23", "portfolio", "all", {
            "apps": [{"id": "a1", "visibility": {"current": 13.9, "delta7d": 1.6}},
                     {"id": "a2", "visibility": {"current": 11.9, "delta7d": None}},
                     {"id": "a3", "visibility": {"current": 16.4, "delta7d": 0.6}},
                     {"id": "zz", "visibility": {"current": 1.0, "delta7d": 0.0}}],  # not in config.toml
            "groups": [{"id": "g1", "name": "Lumen Audiobook Player", "memberAppIds": ["a1", "a2"],
                        "visibility": {"current": 13.2, "delta7d": 1.5}}]})
        db.save_snapshot(conn, "2026-11-23", "portfolio_insights", "all", {
            "apps": [{"appId": "a1", "audit": {"current": 70, "delta7d": 0.2}},
                     {"appId": "a3", "audit": None}]})

    snap = daily_report.snapshot(conn, cfg)

    by_id = {a["id"]: a for a in snap["apps"]}
    assert by_id["lumen-ios"]["visibility"] == {"current": 13.9, "delta7d": 1.6}
    assert by_id["lumen-ios"]["audit"] == {"current": 70, "delta7d": 0.2}
    assert by_id["lumen-android"]["visibility"] == {"current": 11.9, "delta7d": None}
    assert by_id["tandem-ios"]["audit"] is None
    assert "visibility" not in by_id["atlas-ios"]  # not in AsoBeast's answer
    # The group takes the hub's short name, shared by both of its listings.
    assert snap["groups"] == [{"name": "Lumen", "visibility": {"current": 13.2, "delta7d": 1.5}}]


def test_no_portfolio_yet_means_no_visibility(cfg, conn):
    snap = daily_report.snapshot(conn, cfg)
    assert snap["groups"] == [] and all("visibility" not in a for a in snap["apps"])


def test_a_group_with_an_app_no_longer_in_config_is_left_out(cfg, conn):
    with conn:
        db.save_snapshot(conn, "2026-11-23", "ids", "all", {"lumen-ios": "a1", "retired-android": "a2"})
        db.save_snapshot(conn, "2026-11-23", "portfolio", "all", {
            "apps": [{"id": "a1", "visibility": {"current": 13.9, "delta7d": 1.6}},
                     {"id": "a2", "visibility": {"current": 11.9, "delta7d": 1.3}}],
            "groups": [{"id": "g1", "name": "Lumen Audiobook Player", "memberAppIds": ["a1", "a2"],
                        "visibility": {"current": 13.2, "delta7d": 1.5}}]})

    snap = daily_report.snapshot(conn, cfg)

    assert snap["groups"] == []
    assert next(a for a in snap["apps"] if a["id"] == "lumen-ios")["visibility"]["current"] == 13.9
