import gzip
from pathlib import Path

import pytest

from aso_hub import db, settings

ROOT = Path(__file__).resolve().parent.parent


def tsv(header: list[str], rows: list[list]) -> bytes:
    """A gzipped tab-separated file shaped like Apple's reports."""
    lines = ["\t".join(header)] + ["\t".join(str(v) for v in row) for row in rows]
    return gzip.compress(("\n".join(lines) + "\n").encode())


DOWNLOADS_HEADER = ["Date", "App Name", "App Apple Identifier", "Download Type", "App Version", "Device",
                    "Platform Version", "Source Type", "Page Type", "Pre-Order", "Territory", "Counts"]
DISCOVERY_HEADER = ["Date", "App Name", "App Apple Identifier", "Event", "Page Type", "Source Type",
                    "Engagement Type", "Device", "Platform Version", "Territory", "Counts", "Unique Counts"]
INSTALLS_HEADER = ["Date", "App Name", "App Apple Identifier", "Event", "Download Type", "App Version", "Device",
                   "Platform Version", "Source Type", "Page Type", "App Download Date", "Territory", "Counts",
                   "Unique Devices"]
PURCHASES_HEADER = ["Date", "App Name", "App Apple Identifier", "Purchase Type", "Content Name",
                    "Content Apple Identifier", "Payment Method", "Device", "Platform Version", "Source Type",
                    "Page Type", "App Download Date", "Pre-Order", "Territory", "Purchases", "Proceeds in USD",
                    "Sales in USD", "Paying Users"]
SALES_HEADER = ["Provider", "Provider Country", "SKU", "Developer", "Title", "Version", "Product Type Identifier",
                "Units", "Developer Proceeds", "Begin Date", "End Date", "Customer Currency", "Country Code",
                "Currency of Proceeds", "Apple Identifier", "Customer Price", "Promo Code", "Parent Identifier",
                "Subscription", "Period", "Category", "CMB", "Device", "Supported Platforms", "Proceeds Reason",
                "Preserved Pricing", "Client", "Order Type", "Contingent App Name"]


def download_row(date, kind, territory, count, device="iPhone"):
    return [date, "Test App", "111", kind, "1.0", device, "iOS 27.0", "App Store search", "Product page", "",
            territory, count]


def discovery_row(date, event, territory, count, unique, device="iPhone"):
    return [date, "Test App", "111", event, "Product page", "App Store search", "", device, "iOS 27.0", territory,
            count, unique]


def install_row(date, event, territory, count, device="iPhone"):
    return [date, "Test App", "111", event, "First-time download", "1.0", device, "iOS 27.0", "App Store search",
            "Product page", date, territory, count, count]


def purchase_row(date, territory, proceeds, sales):
    return [date, "Test App", "111", "In-app purchase", "Unlock", "222", "Visa", "iPhone", "iOS 27.0",
            "App Store search", "Product page", date, "", territory, 1, proceeds, sales, 1]


def sales_row(app_id, kind, units, territory="US", device="iPhone"):
    row = dict.fromkeys(SALES_HEADER, "")
    row |= {"Product Type Identifier": kind, "Units": units, "Country Code": territory, "Apple Identifier": app_id,
            "Device": device}
    return [row[h] for h in SALES_HEADER]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "hub.db")
    yield c
    c.close()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("HUB_CONFIG", str(ROOT / "config.toml"))
    return settings.load()


def add_sales_day(conn, day: str, rows: dict[tuple[str, str, str, str], float]) -> None:
    """Save one sales day directly: {(app_id, territory, device, metric): units}."""
    with conn:
        conn.executemany("INSERT INTO sales_rows VALUES (?, ?, ?, ?, ?, ?)",
                         [(day, a, t, d, m, v) for (a, t, d, m), v in rows.items()])
        conn.execute("INSERT OR REPLACE INTO sales_days VALUES (?, ?, 1)", (day, db.now()))


def add_analytics(conn, app_id: str, report: str, processing_date: str,
                  rows: dict[tuple[str, str, str, str], tuple[float, float | None]]) -> None:
    """Save one analytics file directly: {(date, territory, device, metric): (value, uniques)}."""
    from aso_hub import asc

    asc.save_instance(conn, app_id, report, processing_date, f"inst-{report}-{processing_date}",
                      {k: list(v) for k, v in rows.items()})
