import dataclasses
import datetime as dt

import httpx
import pytest
from fastapi.testclient import TestClient

from aso_hub import asobeast, db, rules, web
from conftest import add_sales_day

TODAY = dt.date(2026, 10, 28)
LUMEN = "1000000001"


def keyword(kid, text, source="MANUAL", position=10, depth=200):
    return {"keywordId": kid, "text": text, "country": "us", "source": source, "latestPosition": position,
            "latestDepth": depth, "previousPosition": position, "traffic": 2.0, "difficulty": 3.0, "tags": []}


def series(kid, text, positions, country="us", end="2026-10-27"):
    last = dt.date.fromisoformat(end)
    points = [{"date": (last - dt.timedelta(days=len(positions) - 1 - i)).isoformat(), "position": p, "depth": 200}
              for i, p in enumerate(positions)]
    return {"keywordId": kid, "text": text, "store": "APP_STORE", "country": country, "points": points}


class FakeAsoBeast:
    def __init__(self):
        self.calls = []
        self.fail_on = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == self.fail_on:
            return httpx.Response(500)
        details = {"a1": ("APP_STORE", LUMEN), "a2": ("GOOGLE_PLAY", "com.example.lumen"),
                   "x9": ("APP_STORE", "999")}
        if path == "/apps":
            return httpx.Response(200, json=[{"id": i} for i in details])
        if path.count("/") == 2 and path.startswith("/apps/"):
            store, sid = details[path.split("/")[2]]
            return httpx.Response(200, json={"store": store, "storeAppId": sid,
                                             "latestSnapshot": {"ratingAvg": 4.5, "ratingCount": 4}})
        if path == "/actions":
            return httpx.Response(200, json={"items": []})
        if path.endswith("/keywords"):
            return httpx.Response(200, json=[keyword("k1", "offline audiobook")])
        if path.endswith("/rankings"):
            return httpx.Response(200, json={"series": []})
        if path.endswith("/reviews"):
            return httpx.Response(200, json={"reviews": []})
        if path.endswith("/ratings-history"):
            return httpx.Response(200, json={"points": []})
        return httpx.Response(200, json={})


@pytest.fixture
def beast():
    return FakeAsoBeast()


@pytest.fixture
def client(beast):
    return asobeast.Client("http://asobeast-api:4000", "token", transport=httpx.MockTransport(beast))


def test_pull_matches_apps_by_store_id_and_saves_each_market(cfg, conn, client, beast):
    message = asobeast.pull(conn, cfg, client, TODAY)
    assert message == ("read 2 apps; not tracked in AsoBeast: Kettle (Apple), Tandem (Apple), Atlas (Apple), "
                       "Beacon (Android)")
    assert all(r.method == "GET" for r in beast.calls)
    assert db.latest_snapshot(conn, "ids", "all")[0] == {"lumen-ios": "a1", "lumen-android": "a2"}
    saved = {r["key"] for r in conn.execute("SELECT key FROM asobeast_snapshots WHERE kind = 'keywords'")}
    assert saved == {"lumen-ios:us", "lumen-ios:gb", "lumen-ios:de", "lumen-android:us"}
    assert {r.url.params["country"] for r in beast.calls if r.url.path == "/apps/a1/keywords"} == {"us", "gb", "de"}
    # Visibility and audit trends for the Monday post.
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM asobeast_snapshots WHERE key = 'all'")}
    assert {"portfolio", "portfolio_insights"} <= kinds


def test_failed_pull_saves_nothing(cfg, conn, client, beast):
    beast.fail_on = "/apps/a2/reviews"
    with pytest.raises(asobeast.AsoBeastError, match="500"):
        asobeast.pull(conn, cfg, client, TODAY)
    assert conn.execute("SELECT COUNT(*) FROM asobeast_snapshots").fetchone()[0] == 0


def test_refused_token_gives_a_plain_message(cfg, conn):
    client = asobeast.Client("http://x", "t", transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(asobeast.AsoBeastError, match="refused the hub's token"):
        asobeast.pull(conn, cfg, client, TODAY)


@pytest.mark.parametrize("kw, hidden", [
    (keyword("1", "player", "TITLE"), True),
    (keyword("2", "grimmory", "KEYWORD_FIELD"), True),
    (keyword("3", "audiobooks", "MANUAL"), False),          # you added it
    (keyword("4", "audiobook player", "TITLE"), False),     # more than one word
    (keyword("5", "lumen", "TITLE"), False),               # kept in config
])
def test_which_keywords_are_hidden(kw, hidden):
    prefs = {"hide_single_words": True, "keep_single_words": ["lumen"]}
    assert asobeast.is_hidden(kw, prefs) is hidden


def test_hiding_can_be_turned_off():
    assert not asobeast.is_hidden(keyword("1", "player", "TITLE"), {"hide_single_words": False})


def save(conn, kind, key, body, day="2026-10-28"):
    with conn:
        db.save_snapshot(conn, day, kind, key, body)


def only(cfg, *names):
    return dataclasses.replace(cfg, rules={n: r | {"enabled": n in names} for n, r in cfg.rules.items()})


def test_keyword_drop_fires_after_three_days_down(cfg, conn):
    save(conn, "keywords", "lumen-ios:us", [keyword("k1", "offline audiobook"), keyword("k2", "player", "TITLE")])
    save(conn, "rankings", "lumen-ios", {"series": [
        series("k1", "offline audiobook", [12, 10, 11, 12, 10, 11, 12, 30, 31, None]),  # down 3 days
        series("k2", "player", [5, 5, 5, 5, 5, 5, 5, 90, 90, 90]),  # hidden auto word: ignored
        series("k3", "hörbuch", [5, 5, 5, 5, 5, 5, 5, 90, 90, 90], country="fr"),  # not a tracked market
    ]})
    found = rules.findings(only(cfg, "keyword_drop"), conn)
    assert [(f.key, f.title) for f in found] == [("us:k1", "“offline audiobook” dropped in US")]
    detail = found[0].detail
    assert "usually #11 the week before, 15+ places lower on the last 3 checks (#30, #31, not in top 200)" in detail


def test_keyword_drop_quiet_when_it_recovers(cfg, conn):
    save(conn, "keywords", "lumen-ios:us", [keyword("k1", "offline audiobook")])
    save(conn, "rankings", "lumen-ios", {"series": [
        series("k1", "offline audiobook", [10, 10, 10, 10, 10, 10, 10, 30, 12, 31])]})
    assert rules.findings(only(cfg, "keyword_drop"), conn) == []


def test_keyword_drop_ignores_one_good_day(cfg, conn):
    # Usually around #42 with one day at #20: 50, 97, 53 is a swing, not a drop from #20.
    save(conn, "keywords", "lumen-ios:us", [keyword("k1", "audiobook shelf")])
    save(conn, "rankings", "lumen-ios", {"series": [
        series("k1", "audiobook shelf", [40, 42, 45, 20, 44, 41, 43, 50, 97, 53])]})
    assert rules.findings(only(cfg, "keyword_drop"), conn) == []


def test_keyword_drop_needs_a_full_week_before(cfg, conn):
    save(conn, "keywords", "lumen-ios:us", [keyword("k1", "audiobook shelf")])
    save(conn, "rankings", "lumen-ios", {"series": [series("k1", "audiobook shelf", [20, 50, 97, 53])]})
    assert rules.findings(only(cfg, "keyword_drop"), conn) == []


def review(rid, score, day, replied=False):
    return {"reviewId": rid, "score": score, "title": "Nice", "text": "Works with my server.", "version": "1.12",
            "reviewedAt": f"{day}T10:00:00.000Z", "repliedAt": f"{day}T12:00:00.000Z" if replied else None}


def test_new_reviews_and_ratings(cfg, conn, monkeypatch):
    monkeypatch.setattr(rules, "_today", lambda: TODAY)
    save(conn, "reviews", "lumen-ios", {"reviews": [
        review("r1", 2, "2026-10-26"), review("r2", 5, "2026-10-25", replied=True), review("r3", 4, "2026-10-01")]})
    save(conn, "ratings", "lumen-ios", {"points": [
        {"date": "2026-10-25", "ratingAvg": 4.66667, "ratingCount": 3},
        {"date": "2026-10-26", "ratingAvg": 4.5, "ratingCount": 4}]})
    found = rules.findings(only(cfg, "new_review"), conn)
    assert [(f.rule, f.key, f.kind, f.priority) for f in found] == [
        ("new_review", "r1", "do", "medium"), ("new_rating", "2026-10-26", "fyi", "low")]
    assert "3 → 4 ratings" in found[1].detail


def test_low_downloads_names_keywords_that_fell(cfg, conn):
    from aso_hub import metrics

    for d in metrics.days_back("2026-10-27", 7):
        add_sales_day(conn, d, {(LUMEN, "US", "iPhone", "first_time_units"): 1})
    save(conn, "keywords", "lumen-ios:us", [keyword("k1", "offline audiobook")])
    save(conn, "rankings", "lumen-ios", {"series": [
        series("k1", "offline audiobook", [8, 8, 8, 9, 20, 25, 30, 30])]})
    found = rules.findings(only(cfg, "low_downloads"), conn)
    assert "“offline audiobook” #8 → #30" in found[0].detail


def test_app_page_shows_keywords_and_asobeast_actions(cfg):
    conn = db.connect(cfg.database_path)
    save(conn, "ids", "all", {"lumen-ios": "a1"})
    save(conn, "keywords", "lumen-ios:us", [keyword("k1", "offline audiobook", position=46),
                                             keyword("k2", "player", "TITLE", position=None)])
    save(conn, "compare", "lumen-ios", {"competitors": [{"id": "c1", "name": "Rival"}],
                                         "rows": [{"keywordId": "k1", "positions": {"c1": 4}}]})
    save(conn, "audit", "lumen-ios:us", {"fields": [{"field": "title", "value": "Lumen", "chars": 6, "limit": 30}],
                                          "coverage": [{"keywordId": "k1", "uncovered": False,
                                                        "fields": [{"field": "keywordField", "covered": True}]}]})
    save(conn, "actions", "all", {"items": [{
        "rule": "keyword.defend", "status": "OPEN", "priority": "medium", "impact": 39,
        "firstSeenAt": "2026-10-25T03:09:08Z", "scope": {"appId": "a1", "country": "us", "keywordText": "grimmory"},
        "evidence": {"yourPosition": 13, "previousPosition": 22}}]})
    conn.close()
    page = TestClient(web.app)

    text = page.get("/apps/lumen-ios").text
    assert "offline audiobook" in text and "Rival #4" in text and "Keyword field" in text
    assert "1 single word AsoBeast added from the listing" in text
    assert ">player<" not in text
    assert ">player <small>auto</small>" in page.get("/apps/lumen-ios?all=1").text

    actions = page.get("/actions").text
    assert "Defend a keyword" in actions
    assert "“grimmory” · US · you&#39;re #13 (was #22) · impact 39/100" in actions


def test_keyword_drop_ignores_keywords_that_were_far_down(cfg, conn):
    save(conn, "keywords", "lumen-ios:us", [keyword("k1", "rss news")])
    save(conn, "rankings", "lumen-ios", {"series": [
        series("k1", "rss news", [124, 124, 124, 124, 124, 124, 124, None, None, None])]})
    assert rules.findings(only(cfg, "keyword_drop"), conn) == []


def test_words_are_checked_across_fields_and_languages(cfg):
    conn = db.connect(cfg.database_path)
    save(conn, "keywords", "lumen-ios:us", [keyword("k1", "self hosted audiobook"),
                                             keyword("k2", "audiobookshelf app"),
                                             keyword("k3", "audiolibros gratis")])
    save(conn, "audit", "lumen-ios:us", {"fields": [
        {"field": "title", "value": "Lumen Audiobook Player", "indexed": True},
        {"field": "subtitle", "value": "For BookOrbit & Audiobookshelf", "indexed": True},
        {"field": "keywordField", "value": "self,hosted,carplay", "indexed": True},
        {"field": "description", "value": "an app that is gratis", "indexed": False}], "coverage": []})
    save(conn, "market", "lumen-ios:us", {"localizations": [
        {"localization": "en-US", "role": "default", "shipped": True, "phrases": ["self", "hosted"]},
        {"localization": "es-MX", "role": "additional", "shipped": True, "phrases": ["audiolibros"]},
        {"localization": "fr", "role": "additional", "shipped": False, "phrases": []}]})
    conn.close()
    text = " ".join(TestClient(web.app).get("/apps/lumen-ios").text.split())
    assert "all words, across fields" in text           # self + hosted (keyword field) + audiobook (title)
    assert "missing: app" in text
    assert "missing: gratis" in text                    # the description isn't searched
    assert "including the es-MX keyword field" in text


def test_review_says_where_to_reply(cfg, conn, monkeypatch):
    monkeypatch.setattr(rules, "_today", lambda: TODAY)
    save(conn, "reviews", "lumen-android", {"reviews": [review("r1", 5, "2026-10-26")]})
    [found] = rules.findings(only(cfg, "new_review"), conn)
    assert found.detail.endswith("Reply in Play Console (Ratings and reviews).")


def test_play_review_without_a_title_or_rating(cfg):
    conn = db.connect(cfg.database_path)
    save(conn, "app", "lumen-android", {"latestSnapshot": {"ratingAvg": None, "ratingCount": None}})
    save(conn, "reviews", "lumen-android", {"reviews": [review("r1", 5, "2026-10-26") | {"title": None}]})
    conn.close()
    text = TestClient(web.app).get("/apps/lumen-android").text
    assert "Works with my server." in text and "None" not in text
    assert "Google Play isn't showing a star rating yet." in text
