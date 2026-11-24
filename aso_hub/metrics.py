"""Daily numbers per app, ready for the pages and the rules.

Apple leaves out days with nothing to report, so every series here is filled
with zeros between its first and last day.
"""
import datetime as dt
import sqlite3

from . import db

DISCOVERY = "App Store Discovery and Engagement Standard"
INSTALLS = "App Store Installation and Deletion Standard"


def day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def days_back(end: str, count: int) -> list[str]:
    """The `count` days ending on `end`, oldest first."""
    last = day(end)
    return [(last - dt.timedelta(days=i)).isoformat() for i in range(count - 1, -1, -1)]


def total(values: dict[str, float], end: str, count: int) -> float:
    return sum(values.get(d, 0) for d in days_back(end, count))


def shift(text: str, days: int) -> str:
    return (day(text) + dt.timedelta(days=days)).isoformat()


def sum_range(values: dict[str, float], first: str, last: str) -> float:
    return sum(v for d, v in values.items() if first <= d <= last)


# The time ranges the pages offer, in days.
PERIODS = {7: "7 days", 30: "30 days", 90: "3 months", 365: "12 months"}


def buckets(end: str, days: int) -> list[tuple[str, str, str]]:
    """Table rows for a period ending on `end`, newest first, as (label, first day,
    last day): one row a day up to 30 days, a week for 3 months, a month for a year."""
    if days <= 30:
        return [(short_date(d), d, d) for d in reversed(days_back(end, days))]
    if days <= 90:
        rows = []
        for i in range(-(-days // 7)):
            last = shift(end, -7 * i)
            rows.append((f"Week to {short_date(last)}", shift(last, -6), last))
        return rows
    start = shift(end, -(days - 1))
    rows = []
    month = day(end).replace(day=1)
    while True:
        month_end = (month + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
        first, last = max(month.isoformat(), start), min(month_end.isoformat(), end)
        label = f"{month:%b %Y}" + ("" if first == month.isoformat() else f" (from {short_date(first)})")
        rows.append((label, first, last))
        if first == start:
            return rows
        month = (month - dt.timedelta(days=1)).replace(day=1)


def downloads(conn: sqlite3.Connection, apple_id: str, territory: str | None = None) -> dict[str, float]:
    """First-time downloads per day: the larger of the sales report's units
    and the analytics count, as ASO Atlas does (each misses some on some days)."""
    sales = db.daily_totals(conn, apple_id, "first_time_units", territory, table="sales_rows")
    analytics = db.daily_totals(conn, apple_id, "first_time_downloads", territory)
    return {d: max(sales.get(d, 0), analytics.get(d, 0)) for d in sorted(set(sales) | set(analytics))}


def latest_sales_day(conn: sqlite3.Connection) -> str | None:
    """The newest day Apple has published a sales report for."""
    row = conn.execute("SELECT MAX(date) AS d FROM sales_days WHERE found = 1").fetchone()
    return row["d"]


def latest_analytics_day(conn: sqlite3.Connection, apple_id: str, report: str = DISCOVERY) -> str | None:
    """The newest day an analytics report covers for this app."""
    row = conn.execute("SELECT MAX(date) AS d FROM asc_current WHERE app_id = ? AND report = ?",
                       (apple_id, report)).fetchone()
    return row["d"]


def first_analytics_day(conn: sqlite3.Connection, apple_id: str, report: str = DISCOVERY) -> str | None:
    """The oldest day the saved analytics cover (Apple keeps only about 31 days,
    so this starts near the hub's first pull and stays there)."""
    row = conn.execute("SELECT MIN(date) AS d FROM asc_current WHERE app_id = ? AND report = ?",
                       (apple_id, report)).fetchone()
    return row["d"]


def short_date(text: str | None) -> str:
    """'2026-10-27' -> 'Oct 27'."""
    if not text:
        return "no data yet"
    d = day(text[:10])
    return f"{d:%b} {d.day}"
