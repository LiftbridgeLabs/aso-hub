"""Column charts for the app pages, drawn as plain SVG on the server.

One measure per chart, each with its own scale (never two scales on one
chart). The table under the charts has the same numbers.
"""
import datetime as dt
import math

WIDTH, HEIGHT = 320, 120
TOP, BOTTOM = 14, 18  # room for the scale label and the date labels
GAP, MAX_BAR, RADIUS = 2, 24, 4

CHARTS = (("downloads", "Downloads"), ("impressions", "Impressions"),
          ("page_views", "Page views"), ("proceeds", "Proceeds"))


def nice_max(value: float) -> float:
    """The next round number at or above `value` (1, 2 or 5 times a power of ten)."""
    if value <= 0:
        return 1
    power = 10 ** math.floor(math.log10(value))
    return next(step * power for step in (1, 2, 5, 10) if step * power >= value)


def _short(row: dict, days: int) -> str:
    last = dt.date.fromisoformat(row["last"])
    return f"{last:%b %Y}" if days > 90 else f"{last:%b} {last.day}"


def _column(x: float, w: float, h: float) -> str:
    """A column path: square at the baseline, rounded at the data end."""
    base = HEIGHT - BOTTOM
    r = min(RADIUS, w / 2, h)
    top = base - h
    return (f"M{x:.1f},{base} V{top + r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f} V{base} Z")


def column_chart(rows: list[dict], field: str, title: str, days: int, fmt) -> dict:
    """Bars oldest to newest for one field of the period rows (which come newest first)."""
    points = list(reversed(rows))
    values = [p[field] for p in points]
    known = [v for v in values if v is not None]
    top = nice_max(max(known, default=0))
    plot = HEIGHT - TOP - BOTTOM
    slot = WIDTH / max(len(points), 1)
    width = min(MAX_BAR, slot - GAP)
    bars = []
    for i, (point, value) in enumerate(zip(points, values, strict=True)):
        x = i * slot + (slot - width) / 2
        h = plot * (value or 0) / top
        tip = f"{point['label']}: {fmt(value) if value is not None else 'no data yet'}"
        bars.append({"path": _column(x, width, h) if value else None, "hit_x": i * slot, "tip": tip})
    return {"title": title, "field": field, "bars": bars, "slot": slot, "empty": not known,
            "top_label": fmt(top), "first_label": _short(points[0], days) if points else "",
            "last_label": _short(points[-1], days) if points else "",
            "width": WIDTH, "height": HEIGHT, "plot_top": TOP, "base": HEIGHT - BOTTOM}


PLAY_CHARTS = (("installs", "Installs (new users)"), ("visitors", "Store listing visitors"),
               ("uninstalls", "Uninstalls"), ("purchases", "In-app purchases"))


def charts(rows: list[dict], days: int, num, money, which=CHARTS) -> list[dict]:
    return [column_chart(rows, field, title, days, money if field == "proceeds" else num)
            for field, title in which]


SPARK_W, SPARK_H = 120, 32


def sparkline(rows: list[dict], field: str) -> dict | None:
    """A small trend line for the overview, oldest to newest, with no axes (the
    number beside it gives the scale). Missing values count as zero."""
    values = [r[field] or 0 for r in reversed(rows)]
    if len(values) < 2 or not any(values):
        return None
    top = max(values)
    step = SPARK_W / (len(values) - 1)
    points = [(i * step, SPARK_H - 2 - (SPARK_H - 4) * v / top) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return {"line": line, "area": f"M0,{SPARK_H} L{line.replace(' ', ' L')} L{SPARK_W},{SPARK_H} Z",
            "width": SPARK_W, "height": SPARK_H, "last": points[-1]}
