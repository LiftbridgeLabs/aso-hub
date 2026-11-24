from aso_hub import charts, web


def rows(values):
    """Period rows newest first, as store_numbers builds them."""
    from aso_hub import metrics

    days = metrics.days_back("2026-10-27", len(values))
    out = [{"label": metrics.short_date(d), "first": d, "last": d, "downloads": v}
           for d, v in zip(days, values, strict=True)]
    return list(reversed(out))


def test_scale_rounds_up():
    assert [charts.nice_max(v) for v in (0, 3, 12, 45, 101, 5000)] == [1, 5, 20, 50, 200, 5000]


def test_columns_run_oldest_to_newest_and_skip_missing_days():
    chart = charts.column_chart(rows([1, 2, None, 4, 0, 6, 12]), "downloads", "Downloads", 7, web.num)
    assert chart["first_label"] == "Oct 21" and chart["last_label"] == "Oct 27"
    assert chart["top_label"] == "20"
    assert [b["path"] is not None for b in chart["bars"]] == [True, True, False, True, False, True, True]
    assert chart["bars"][2]["tip"] == "Oct 23: no data yet"
    assert chart["bars"][-1]["tip"] == "Oct 27: 12"


def test_a_period_with_no_numbers_says_so():
    assert charts.column_chart(rows([None] * 7), "downloads", "Downloads", 7, web.num)["empty"]


def test_sparkline_runs_oldest_to_newest_and_skips_flat_zero():
    line = charts.sparkline(rows([0, 2, 4]), "downloads")
    xs = [float(p.split(",")[0]) for p in line["line"].split()]
    assert xs == sorted(xs) and line["last"][1] == 2  # newest (4, the top) sits at the right, at the top
    assert charts.sparkline(rows([0, 0, 0]), "downloads") is None
    assert charts.sparkline(rows([5]), "downloads") is None
