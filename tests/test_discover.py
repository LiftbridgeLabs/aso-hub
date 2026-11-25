import dataclasses
import datetime as dt

import httpx
import pytest
from fastapi.testclient import TestClient

from aso_hub import asobeast, collect, discover, settings, web
from conftest import ROOT

TODAY = dt.date(2026, 10, 28)


def beast(apps: dict[str, dict], countries: list[str] = ("de", "us")):
    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/apps":
            return httpx.Response(200, json=[{"id": i} for i in apps])
        if path.count("/") == 2 and path.startswith("/apps/"):
            return httpx.Response(200, json=apps[path.split("/")[2]])
        if path.endswith("/keyword-countries"):
            return httpx.Response(200, json=[{"country": c, "keywordCount": 1} for c in countries])
        if path.endswith("/keywords"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    return asobeast.Client("http://asobeast-api:4000", "token", transport=httpx.MockTransport(answer))


def listing(title, sid, store="APP_STORE", **more):
    return {"name": title, "store": store, "storeAppId": sid, "country": "us"} | more


@pytest.fixture
def empty(cfg):
    """A fresh install: no apps in config.toml."""
    return dataclasses.replace(cfg, apps=[])


def test_an_app_tracked_in_asobeast_joins_the_hub(conn, empty):
    apps = {"a": listing("Notes: Quick Memos", "123456789"),
            "b": listing("Notes - Quick Memos", "com.example.notes", "GOOGLE_PLAY")}
    message = asobeast.pull(conn, empty, beast(apps), TODAY)
    assert message == "read 2 apps (2 added from AsoBeast)"
    found = {a.slug: a for a in discover.with_apps(empty, conn).apps}
    assert set(found) == {"notes-ios", "notes-android"}
    assert found["notes-ios"].label == "Notes (Apple)" and found["notes-ios"].apple_id == "123456789"
    assert found["notes-ios"].markets == ["us", "de"]  # the home storefront first
    assert found["notes-android"].google_package == "com.example.notes"


def test_config_wins_and_ignored_apps_stay_out(conn, cfg):
    lumen = cfg.app("lumen-ios")
    mine = dataclasses.replace(cfg, apps=[lumen], ignore_apps=frozenset({"222222222"}))
    apps = {"a": listing("Another title", lumen.apple_id), "b": listing("Skip me", "222222222"),
            "c": listing("Gone", "333333333", delistedAt="2026-10-01"),
            "d": listing("Bad id", "../v1/users"), "e": listing("New", "444444444")}
    asobeast.pull(conn, mine, beast(apps), TODAY)
    assert [a.slug for a in discover.with_apps(mine, conn).apps] == ["lumen-ios", "new-ios"]


def test_an_app_keeps_its_address_when_its_title_changes(conn, empty):
    asobeast.pull(conn, empty, beast({"a": listing("Old Name", "123456789")}), TODAY)
    asobeast.pull(conn, discover.with_apps(empty, conn), beast({"a": listing("New Name", "123456789")}),
                  TODAY + dt.timedelta(days=1))
    [app] = discover.with_apps(empty, conn).apps
    assert (app.slug, app.name) == ("old-name-ios", "New Name")


def test_two_apps_with_the_same_short_name_keep_their_full_titles(conn, empty):
    apps = {"a": listing("Notes: Work", "111111111"), "b": listing("Notes: Home", "222222222")}
    asobeast.pull(conn, empty, beast(apps), TODAY)
    assert [a.name for a in discover.with_apps(empty, conn).apps] == ["Notes: Home", "Notes: Work"]


def test_adding_can_be_turned_off(conn, empty):
    asobeast.pull(conn, empty, beast({"a": listing("Notes", "123456789")}), TODAY)
    assert discover.with_apps(dataclasses.replace(empty, add_apps=False), conn).apps == []


def test_a_fresh_install_starts_with_no_apps_and_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("HUB_CONFIG", str(ROOT / "config.toml"))
    fresh = settings.load()
    assert fresh.apps == [] and fresh.add_apps
    page = TestClient(web.app)
    assert "No apps yet" in page.get("/").text
    assert page.get("/actions").status_code == 200


def test_store_pulls_are_skipped_without_apps_for_that_store(conn, empty):
    assert collect._pull_asc(empty, conn) == "no App Store apps to read"
    assert collect._pull_play(empty, conn) == "no Google Play apps to read"
