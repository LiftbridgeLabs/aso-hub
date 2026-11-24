import dataclasses
import json

import httpx
import pytest

from aso_hub import db, notify

WEBHOOK = "https://discord.com/api/webhooks/123/secret-part"


class FakeDiscord:
    def __init__(self, status=200):
        self.posts = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params["wait"] == "true"
        self.posts.append(json.loads(request.content))
        return httpx.Response(self.status, json={"id": "1"})


@pytest.fixture
def hooked(cfg, tmp_path):
    path = tmp_path / "discord-webhook"
    path.write_text(WEBHOOK + "\n")
    return dataclasses.replace(cfg, discord_webhook_file=path)


def add_action(conn, key, kind="do", priority="medium", first_seen="2026-10-28T13:00:00+00:00", resolved=None):
    with conn:
        conn.execute("INSERT OR REPLACE INTO hub_actions (rule, app_slug, key, title, detail, priority, kind, "
                     "first_seen, last_seen, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("keyword_drop", "lumen-ios", key, f"Title {key}", f"Detail {key}", priority, kind,
                      first_seen, first_seen, resolved))


def add_asobeast(conn, items):
    with conn:
        db.save_snapshot(conn, "2026-10-28", "ids", "all", {"lumen-android": "a2"})
        db.save_snapshot(conn, "2026-10-28", "actions", "all", {"items": items})


def beast_item(iid, status="OPEN", reopen=0):
    return {"id": iid, "rule": "keyword.defend", "status": status, "priority": "high", "reopenCount": reopen,
            "firstSeenAt": "2026-10-25T03:09:08Z", "scope": {"appId": "a2", "country": "us", "keywordText": "x"},
            "evidence": {}}


def test_new_todos_post_once(hooked, conn):
    add_action(conn, "us:k1", priority="low")
    add_action(conn, "us:k2", kind="fyi")
    add_asobeast(conn, [beast_item("b1"), beast_item("b2", status="SNOOZED")])
    discord = FakeDiscord()
    transport = httpx.MockTransport(discord)

    assert notify.send(conn, hooked, transport) == "posted 2 new to-dos"
    [post] = discord.posts
    text = post["content"]
    assert text.startswith("**ASO Hub: 2 new to-dos**")
    assert text.index("Defend a keyword") < text.index("Title us:k1")  # high before low
    assert "Title us:k2" not in text                                    # worth knowing stays on the page
    assert text.endswith("<http://localhost:8690/actions>")
    assert post["allowed_mentions"] == {"parse": []} and post["flags"] == 4

    assert notify.send(conn, hooked, transport) == "no new to-dos"
    assert len(discord.posts) == 1


def test_a_reopened_action_posts_again(hooked, conn):
    discord = FakeDiscord()
    transport = httpx.MockTransport(discord)
    add_action(conn, "us:k1")
    add_asobeast(conn, [beast_item("b1")])
    notify.send(conn, hooked, transport)

    add_action(conn, "us:k1", first_seen="2026-10-30T13:00:00+00:00")
    add_asobeast(conn, [beast_item("b1", reopen=1)])
    assert notify.send(conn, hooked, transport) == "posted 2 new to-dos"


def test_failed_post_is_retried_and_never_shows_the_address(hooked, conn):
    add_action(conn, "us:k1")
    with pytest.raises(notify.NotifyError, match="HTTP 500") as err:
        notify.send(conn, hooked, httpx.MockTransport(FakeDiscord(status=500)))
    assert "secret-part" not in str(err.value)

    def unreachable(request):
        raise httpx.ConnectError(f"can't reach {request.url}")

    with pytest.raises(notify.NotifyError) as err:
        notify.send(conn, hooked, httpx.MockTransport(unreachable))
    assert "secret-part" not in str(err.value)
    assert notify.send(conn, hooked, httpx.MockTransport(FakeDiscord())) == "posted 1 new to-do"


def test_no_webhook_posts_nothing(cfg, conn):
    add_action(conn, "us:k1")
    assert notify.send(conn, cfg, httpx.MockTransport(FakeDiscord())) == "no Discord webhook saved; nothing posted"


def test_a_wrong_address_is_refused(hooked, conn):
    hooked.discord_webhook_file.write_text("https://example.com/hook")
    with pytest.raises(notify.NotifyError, match="isn't a Discord webhook"):
        notify.send(conn, hooked, httpx.MockTransport(FakeDiscord()))


def test_a_long_list_goes_out_in_several_messages(hooked, conn):
    for i in range(30):
        add_action(conn, f"us:k{i:02}")
    conn.execute("UPDATE hub_actions SET detail = ?", ("d" * 400,))
    discord = FakeDiscord()
    assert notify.send(conn, hooked, httpx.MockTransport(discord)) == "posted 30 new to-dos"
    assert len(discord.posts) > 1
    assert all(len(p["content"]) <= notify.LIMIT for p in discord.posts)
    text = "".join(p["content"] for p in discord.posts)
    assert all(f"Title us:k{i:02}" in text for i in range(30))


def test_a_failed_second_message_keeps_its_items_for_next_time(hooked, conn):
    for i in range(30):
        add_action(conn, f"us:k{i:02}")
    conn.execute("UPDATE hub_actions SET detail = ?", ("d" * 400,))
    answers = iter([200, 500])
    transport = httpx.MockTransport(lambda r: httpx.Response(next(answers), json={"id": "1"}))
    with pytest.raises(notify.NotifyError):
        notify.send(conn, hooked, transport)
    done = conn.execute("SELECT COUNT(*) FROM sent_alerts").fetchone()[0]
    assert 0 < done < 30
    assert notify.send(conn, hooked, httpx.MockTransport(FakeDiscord())) == f"posted {30 - done} new to-dos"
