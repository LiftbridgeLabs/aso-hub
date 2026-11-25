import dataclasses
import datetime as dt

import httpx
import pytest

from aso_hub import asc, db, metrics
from conftest import (
    DISCOVERY_HEADER,
    DOWNLOADS_HEADER,
    INSTALLS_HEADER,
    PURCHASES_HEADER,
    SALES_HEADER,
    add_analytics,
    discovery_row,
    download_row,
    install_row,
    purchase_row,
    sales_row,
    tsv,
)


def test_reads_each_report_kind():
    totals = asc.new_totals()
    asc.add_report("App Downloads Standard", tsv(DOWNLOADS_HEADER, [
        download_row("2026-10-27", "First-time download", "US", 2),
        download_row("2026-10-27", "First-time download", "US", 1),
        download_row("2026-10-27", "Redownload", "US", 4),
        download_row("2026-10-27", "Auto-update", "US", 9),  # updates aren't downloads
    ]), totals)
    assert totals[("2026-10-27", "US", "iPhone", "first_time_downloads")] == [3.0, None]
    assert totals[("2026-10-27", "US", "iPhone", "redownloads")] == [4.0, None]
    assert len(totals) == 2

    totals = asc.add_report("App Store Discovery and Engagement Standard", tsv(DISCOVERY_HEADER, [
        discovery_row("2026-10-27", "Impression", "US", 10, 7),
        discovery_row("2026-10-27", "Page view", "US", 5, 2),
        discovery_row("2026-10-27", "Tap", "US", 99, 99),
    ]), asc.new_totals())
    assert totals[("2026-10-27", "US", "iPhone", "impressions")] == [10.0, 7.0]
    assert totals[("2026-10-27", "US", "iPhone", "page_views")] == [5.0, 2.0]
    assert len(totals) == 2

    totals = asc.add_report("App Store Installation and Deletion Standard", tsv(INSTALLS_HEADER, [
        install_row("2026-10-26", "Install", "GB", 3), install_row("2026-10-26", "Delete", "GB", 1),
    ]), asc.new_totals())
    assert totals[("2026-10-26", "GB", "iPhone", "deletions")] == [1.0, 1.0]

    totals = asc.add_report("App Store Purchases Standard", tsv(PURCHASES_HEADER, [
        purchase_row("2026-10-27", "DE", "6.38", "10.27"),
    ]), asc.new_totals())
    assert totals[("2026-10-27", "DE", "iPhone", "proceeds_usd")][0] == pytest.approx(6.38)


def test_newest_file_for_a_day_wins(conn):
    # Apple restates a day in the next day's file; adding the two would double count.
    report, metric = "App Downloads Standard", "first_time_downloads"
    add_analytics(conn, "111", report, "2026-10-26", {("2026-10-25", "US", "iPhone", metric): (3, None)})
    add_analytics(conn, "111", report, "2026-10-27", {("2026-10-25", "US", "iPhone", metric): (4, None),
                                                      ("2026-10-26", "US", "iPhone", metric): (2, None)})
    assert db.daily_totals(conn, "111", "first_time_downloads") == {"2026-10-25": 4, "2026-10-26": 2}


def test_downloads_take_the_larger_of_sales_and_analytics(conn):
    from conftest import add_sales_day

    add_sales_day(conn, "2026-10-21", {("111", "US", "iPhone", "first_time_units"): 7})
    add_sales_day(conn, "2026-10-22", {("111", "US", "iPhone", "first_time_units"): 2})
    add_analytics(conn, "111", "App Downloads Standard", "2026-10-23", {
        ("2026-10-21", "US", "iPhone", "first_time_downloads"): (3, None),
        ("2026-10-22", "US", "iPhone", "first_time_downloads"): (5, None)})
    assert metrics.downloads(conn, "111") == {"2026-10-21": 7, "2026-10-22": 5}


def test_sales_report_counts_only_downloads():
    totals = asc.parse_sales(tsv(SALES_HEADER, [
        sales_row("111", "1", 2), sales_row("111", "1F", 1), sales_row("111", "3", 4),
        sales_row("111", "7", 39),  # update
        sales_row("222", "IA1", 1),  # in-app purchase
    ]))
    assert totals == {("111", "US", "iPhone", "first_time_units"): 3.0,
                      ("111", "US", "iPhone", "redownload_units"): 4.0}


REPORT_NAMES = list(asc.REPORTS)
EMPTY_FILES = {
    "App Store Discovery and Engagement Standard": DISCOVERY_HEADER,
    "App Store Installation and Deletion Standard": INSTALLS_HEADER,
    "App Store Purchases Standard": PURCHASES_HEADER,
}


class FakeApple:
    """Answers the few App Store Connect calls the hub makes, and records them."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.days = ["2026-10-01", "2026-10-27"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        parts = path.split("/")
        if request.url.host.endswith("amazonaws.com"):
            assert "authorization" not in request.headers  # our token never goes to the file host
            name, day = REPORT_NAMES[int(parts[2])], parts[3]
            if name == "App Downloads Standard":
                return httpx.Response(200, content=tsv(DOWNLOADS_HEADER, [
                    download_row(day, "First-time download", "US", 1)]))
            return httpx.Response(200, content=tsv(EMPTY_FILES[name], []))
        if path == "/v1/apps/111/analyticsReportRequests":
            return httpx.Response(200, json={"data": [
                {"id": "snap", "attributes": {"accessType": "ONE_TIME_SNAPSHOT", "stoppedDueToInactivity": False}},
                {"id": "ongoing", "attributes": {"accessType": "ONGOING", "stoppedDueToInactivity": False}}]})
        if path == "/v1/analyticsReportRequests/ongoing/reports":
            return httpx.Response(200, json={"data": [
                {"id": str(i), "attributes": {"name": n}} for i, n in enumerate(REPORT_NAMES)]})
        if path.startswith("/v1/analyticsReports/"):  # /v1/analyticsReports/<report>/instances
            return httpx.Response(200, json={"data": [
                {"id": f"{parts[3]}_{d}", "attributes": {"processingDate": d}} for d in self.days]})
        if path.startswith("/v1/analyticsReportInstances/"):
            report, day = parts[3].split("_")
            return httpx.Response(200, json={"data": [
                {"attributes": {"url": f"https://bucket.s3.us-west-2.amazonaws.com/files/{report}/{day}"}}]})
        if path == "/v1/salesReports":
            if request.url.params["filter[reportDate]"] == "2026-10-26":
                return httpx.Response(200, content=tsv(SALES_HEADER, [sales_row("111", "1", 2)]))
            return httpx.Response(404, json={"errors": [{"code": "NOT_FOUND"}]})
        return httpx.Response(500)


@pytest.fixture
def apple():
    return FakeApple()


@pytest.fixture
def client(apple):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return asc.Client("issuer", "KEYID", key, transport=httpx.MockTransport(apple))


def test_pull_reads_new_files_and_rereads_recent_ones(conn, client, apple):
    today = dt.date(2026, 10, 28)
    assert asc.pull_app(conn, client, "111", today) == 8  # 2 files x 4 reports
    assert all(r.method == "GET" for r in apple.calls)  # never creates report requests
    apple.calls.clear()
    # The Oct 1 files are saved and old; only the recent Oct 27 files are read again.
    assert asc.pull_app(conn, client, "111", today) == 4
    assert db.daily_totals(conn, "111", "first_time_downloads") == {"2026-10-01": 1, "2026-10-27": 1}


def test_pull_stops_without_an_ongoing_request(conn, client, apple):
    original = apple.__call__

    def no_ongoing(request):
        if request.url.path == "/v1/apps/111/analyticsReportRequests":
            return httpx.Response(200, json={"data": []})
        return original(request)

    client._api._transport = httpx.MockTransport(no_ongoing)
    with pytest.raises(asc.AscError, match="no active ongoing"):
        asc.pull_app(conn, client, "111")


def test_sales_pull_records_missing_days_and_rereads_recent(conn, client, apple, monkeypatch):
    monkeypatch.setattr(asc, "SALES_HISTORY_DAYS", 10)
    today = dt.date(2026, 10, 28)
    assert asc.pull_sales(conn, client, "999", today) == 10
    found = {r["date"]: r["found"] for r in conn.execute("SELECT * FROM sales_days")}
    assert found["2026-10-26"] == 1 and found["2026-10-20"] == 0
    assert metrics.latest_sales_day(conn) == "2026-10-26"
    # Second pull: only the last REFRESH_DAYS are asked for again.
    assert asc.pull_sales(conn, client, "999", today) == asc.REFRESH_DAYS


def test_refused_key_gives_a_plain_message(conn, client):
    client._api._transport = httpx.MockTransport(lambda r: httpx.Response(401))
    with pytest.raises(asc.AscError, match="refused"):
        asc.pull_app(conn, client, "111")


def test_apple_server_errors_are_retried_then_named_as_apples(conn, client, monkeypatch):
    monkeypatch.setattr(asc, "SERVER_ERROR_WAIT", (0, 0))
    calls = []

    def flaky(request):
        calls.append(request)
        return httpx.Response(500) if len(calls) < 3 else httpx.Response(200, json={"data": []})

    client._api._transport = httpx.MockTransport(flaky)
    assert client.get("/v1/apps/111/analyticsReportRequests") == {"data": []}
    assert len(calls) == 3

    client._api._transport = httpx.MockTransport(lambda r: httpx.Response(500))
    with pytest.raises(asc.AscError, match="on Apple's side"):
        client.get("/v1/apps/111/analyticsReportRequests")


def test_an_app_without_a_report_request_doesnt_stop_the_others(conn, client, apple, cfg, monkeypatch):
    from aso_hub import collect

    original = apple.__call__

    def only_111(request):
        if request.url.path == "/v1/apps/222/analyticsReportRequests":
            return httpx.Response(200, json={"data": []})
        return original(request)

    client._api._transport = httpx.MockTransport(only_111)
    monkeypatch.setattr(asc, "Client", lambda *a, **k: client)
    monkeypatch.setattr(asc, "pull_sales", lambda *a, **k: 0)
    apps = [dataclasses.replace(cfg.apps[0], slug="old", name="Old", apple_id="111"),
            dataclasses.replace(cfg.apps[0], slug="new", name="New", apple_id="222")]
    settings = dataclasses.replace(cfg, apps=apps, asc_issuer_id="i", asc_key_id="k", asc_vendor_number="v")
    monkeypatch.setattr(type(settings), "read_secret", lambda self, path: "key")
    message = collect._pull_asc(settings, conn)
    assert message.startswith("read 0 sales days and ")
    assert message.endswith("; no store page reports requested yet: New (Apple)")
    assert db.daily_totals(conn, "111", "first_time_downloads")


def test_a_stopped_request_is_a_failure_not_a_new_app(conn, client, apple):
    original = apple.__call__

    def stopped(request):
        if request.url.path == "/v1/apps/111/analyticsReportRequests":
            return httpx.Response(200, json={"data": [
                {"id": "old", "attributes": {"accessType": "ONGOING", "stoppedDueToInactivity": True}}]})
        return original(request)

    client._api._transport = httpx.MockTransport(stopped)
    with pytest.raises(asc.AscError, match="Apple stopped") as e:
        asc.pull_app(conn, client, "111")
    assert not isinstance(e.value, asc.NoReportRequest)


def test_one_apps_failure_doesnt_stop_the_apps_after_it(conn, cfg, monkeypatch):
    from aso_hub import collect

    read = []

    def pull_app(conn, client, app_id):
        if app_id == "111":
            raise asc.AscError("Apple stopped the analytics reports")
        read.append(app_id)
        return 1

    monkeypatch.setattr(asc, "Client", lambda *a, **k: None)
    monkeypatch.setattr(asc, "pull_sales", lambda *a, **k: 0)
    monkeypatch.setattr(asc, "pull_app", pull_app)
    apps = [dataclasses.replace(cfg.apps[0], slug="bad", name="Bad", apple_id="111"),
            dataclasses.replace(cfg.apps[0], slug="good", name="Good", apple_id="222")]
    settings = dataclasses.replace(cfg, apps=apps, asc_issuer_id="i", asc_key_id="k", asc_vendor_number="v")
    monkeypatch.setattr(type(settings), "read_secret", lambda self, path: "key")
    with pytest.raises(asc.AscError, match=r"Bad \(Apple\): Apple stopped"):
        collect._pull_asc(settings, conn)
    assert read == ["222"]
