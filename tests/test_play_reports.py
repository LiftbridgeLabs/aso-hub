import dataclasses
import io
import zipfile
from urllib.parse import unquote

import httpx
from fastapi.testclient import TestClient
from test_play import KEY

from aso_hub import db, play, play_reports, web

PKG = "com.example.lumen"
BUCKET = "pubsite_prod_1"


def utf16(text: str) -> bytes:
    return text.encode("utf-16")  # with a byte-order mark, as Play writes them


def zipped(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, "﻿" + text)
    return buf.getvalue()


INSTALLS = ("Date,Package name,Daily Device Installs,Daily Device Uninstalls,Daily Device Upgrades,Total User Installs,"
            "Daily User Installs,Daily User Uninstalls,Active Device Installs,Install events,Update events,"
            "Uninstall events\n"
            f"2026-10-25,{PKG},0,0,0,0,0,0,44,0,0,0\n"
            f"2026-10-26,{PKG},2,0,0,0,2,1,40,2,0,2\n")
COUNTRY = ("Date,Package name,Country,Daily Device Installs,Daily Device Uninstalls,Daily Device Upgrades,Total User "
           "Installs,Daily User Installs,Daily User Uninstalls,Active Device Installs,Install events,Update events,"
           "Uninstall events\n"
           f"2026-10-26,{PKG},US,1,0,0,0,1,1,12,1,0,1\n"
           f"2026-10-26,{PKG},SE,1,0,0,0,1,0,1,1,0,0\n")
STORE = ("Date,Package name,Country / region,Store listing acquisitions,Store listing visitors,"
         "Store listing conversion rate\n"
         f"2026-10-24,{PKG},Other,2,8,0.25\n"
         f"2026-10-24,{PKG},US,1,4,0.25\n")
SALES = ("Order Number,Order Charged Date,Financial Status,Product Title,Package ID,Currency of Sale,Item Price,"
         "City of Buyer,Postal Code of Buyer\n"
         f"GPA.1,2026-10-26,Charged,Lumen Unlock,{PKG},DKK,25.60,Copenhagen,1000\n"
         f"GPA.2,2026-10-26,Charged,Lumen Unlock,{PKG},USD,3.99,Denver,80202\n"
         f"GPA.3,2026-10-27,Refund,Lumen Unlock,{PKG},USD,3.99,Denver,80202\n"
         "GPA.4,2026-10-26,Charged,Other app,com.example.other,USD,1.99,Austin,73301\n")
EARNINGS = ("Description,Transaction Date,Transaction Type,Package ID,Buyer Postal Code,Amount (Merchant Currency)\n"
            f"GPA.1,\"Sep 19, 2026\",Charge,{PKG},SW1A,3.90\n"
            f"GPA.1,\"Sep 19, 2026\",Google fee,{PKG},SW1A,-0.59\n")

FILES = {
    f"stats/installs/installs_{PKG}_202610_overview.csv": utf16(INSTALLS),
    f"stats/installs/installs_{PKG}_202610_country.csv": utf16(COUNTRY),
    f"stats/installs/installs_{PKG}_202610_device.csv": utf16("not read"),
    f"stats/store_performance/store_performance_{PKG}_202610_country.csv": utf16(STORE),
    "sales/salesreport_202610.zip": zipped("salesreport_202610.csv", SALES),
    "earnings/earnings_202609_1-1.zip": zipped("PlayApps_202609.csv", EARNINGS),
}


class FakeBucket:
    def __init__(self):
        self.files = dict(FILES)
        self.updated = dict.fromkeys(FILES, "2026-10-30T10:00:00Z")
        self.downloads = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        path = request.url.path
        if path.startswith("/v1beta1/"):
            return httpx.Response(200, json={"errorIssues": []})
        if path == f"/storage/v1/b/{BUCKET}/o":
            return httpx.Response(200, json={"items": [{"name": n, "updated": self.updated[n]} for n in self.files]})
        name = unquote(request.url.raw_path.decode().split("/o/", 1)[1].split("?")[0])
        self.downloads.append(name)
        return httpx.Response(200, content=self.files[name])


def setup(cfg, conn):
    bucket = FakeBucket()
    client = play.Client(KEY, transport=httpx.MockTransport(bucket))
    return dataclasses.replace(cfg, play_bucket=BUCKET), client, bucket


def test_reads_each_report_and_keeps_only_totals(cfg, conn):
    cfg, client, bucket = setup(cfg, conn)
    assert play_reports.pull(conn, cfg, client) == "read 5 report files"  # the device file isn't needed
    app = cfg.app("lumen-android")
    assert play_reports.daily(conn, app, "user_installs") == {"2026-10-25": 0, "2026-10-26": 2}
    assert play_reports.daily(conn, app, "user_installs", "US") == {"2026-10-26": 1}
    assert play_reports.daily(conn, app, "visitors") == {"2026-10-24": 12}      # summed over countries
    assert play_reports.daily(conn, app, "purchases") == {"2026-10-26": 2}      # other apps' orders left out
    assert play_reports.daily(conn, app, "refunds") == {"2026-10-27": 1}
    assert play_reports.daily(conn, app, "earnings_net_usd") == {"2026-09-01": 3.31}
    stored = " ".join(str(v) for r in conn.execute("SELECT * FROM play_rows") for v in tuple(r))
    assert "Denver" not in stored and "80202" not in stored and "SW1A" not in stored


def test_unchanged_files_are_skipped_and_rewritten_ones_replaced(cfg, conn):
    cfg, client, bucket = setup(cfg, conn)
    play_reports.pull(conn, cfg, client)
    bucket.downloads.clear()
    assert play_reports.pull(conn, cfg, client) == "read 0 report files"
    name = f"stats/installs/installs_{PKG}_202610_overview.csv"
    bucket.files[name] = utf16(INSTALLS.replace(f"2026-10-26,{PKG},2,0,0,0,2,1", f"2026-10-26,{PKG},3,0,0,0,3,1"))
    bucket.updated[name] = "2026-10-31T10:00:00Z"
    assert play_reports.pull(conn, cfg, client) == "read 1 report file"
    # replaced, not added to
    assert play_reports.daily(conn, cfg.app("lumen-android"), "user_installs")["2026-10-26"] == 3


def test_play_pull_reads_reports_after_crashes(cfg, conn):
    cfg, client, bucket = setup(cfg, conn)
    assert play.pull(conn, cfg, client) == "read 0 crash and ANR issues; read 5 report files"


def test_android_page_shows_play_numbers(cfg):
    conn = db.connect(cfg.database_path)
    cfg2, client, _ = setup(cfg, conn)
    play_reports.pull(conn, cfg2, client)
    conn.close()
    page = TestClient(web.app)
    text = " ".join(page.get("/apps/lumen-android").text.split())
    assert "7 days to Oct 26" in text and "Store listing visitors" in text and "Devices with Lumen" in text
    assert "$3.31" in text and "Sep 2026" in text
    assert "Installs, 7 days to Oct 26" in page.get("/").text


def test_a_new_app_rereads_the_account_wide_files(cfg, conn):
    cfg, client, bucket = setup(cfg, conn)
    play_reports.pull(conn, cfg, client)
    extra = dataclasses.replace(cfg.app("lumen-android"), slug="other-android", google_package="com.example.other")
    cfg = dataclasses.replace(cfg, apps=[*cfg.apps, extra])
    bucket.downloads.clear()
    play_reports.pull(conn, cfg, client)
    assert "sales/salesreport_202610.zip" in bucket.downloads
    assert play_reports.daily(conn, extra, "purchases") == {"2026-10-26": 1}


def test_other_country_header_spellings():
    raw = utf16("Date,Package name,Country/region,Store listing acquisitions,Store listing visitors\n"
                f"2026-10-24,{PKG},US,1,4\n")
    rows = play_reports.parse(f"stats/store_performance/store_performance_{PKG}_202610_country.csv", raw, PKG)
    assert ("2026-10-24", "US", "visitors", 4.0) in rows
