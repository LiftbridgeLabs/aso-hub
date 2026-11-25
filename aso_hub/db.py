"""The hub's SQLite database. Nothing is ever deleted: it is also the history."""
import datetime as dt
import json
import sqlite3
from pathlib import Path

SCHEMA = """
-- One row per Apple analytics file the hub has read. Apple keeps about 31
-- days of daily files, so this table is the only copy once they expire.
CREATE TABLE IF NOT EXISTS asc_instances (
    app_id TEXT NOT NULL,
    report TEXT NOT NULL,
    processing_date TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (app_id, report, processing_date)
);

-- The numbers from those files, summed by day, country and device. Apple
-- restates a day in the next day's file (sometimes revised upward), so the
-- newest file that covers a day is the one that counts: see asc_current.
CREATE TABLE IF NOT EXISTS asc_rows (
    app_id TEXT NOT NULL,
    report TEXT NOT NULL,
    processing_date TEXT NOT NULL,
    date TEXT NOT NULL,
    territory TEXT NOT NULL,
    device TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    uniques REAL,
    PRIMARY KEY (app_id, report, processing_date, date, territory, device, metric)
);
CREATE INDEX IF NOT EXISTS asc_rows_by_day ON asc_rows (app_id, metric, date);

CREATE VIEW IF NOT EXISTS asc_current AS
SELECT r.* FROM asc_rows r
JOIN (SELECT app_id, report, date, MAX(processing_date) AS newest FROM asc_rows GROUP BY app_id, report, date) n
  ON r.app_id = n.app_id AND r.report = n.report AND r.date = n.date AND r.processing_date = n.newest;

-- Apple's daily Sales and Trends report: first-time and repeat download units
-- for every app on the account. Its counts are higher than the analytics
-- files on some days; the hub uses the larger of the two, as ASO Atlas does.
CREATE TABLE IF NOT EXISTS sales_rows (
    date TEXT NOT NULL,
    app_id TEXT NOT NULL,
    territory TEXT NOT NULL,
    device TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (date, app_id, territory, device, metric)
);

-- Which sales days have been read. found = 0 means Apple had no report
-- (nothing sold that day, or not published yet; recent days are re-read).
CREATE TABLE IF NOT EXISTS sales_days (
    date TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    found INTEGER NOT NULL
);

-- Every pull from every source, so pages can say how fresh each one is.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER,
    message TEXT
);

-- What AsoBeast and Google Play returned, one copy per day and kind (apps,
-- keywords, play_issues, ...).
CREATE TABLE IF NOT EXISTS asobeast_snapshots (
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (day, kind, key)
);

-- The hub's own actions. An action stays open while its rule keeps firing
-- and is resolved the first pull it doesn't.
CREATE TABLE IF NOT EXISTS hub_actions (
    rule TEXT NOT NULL,
    app_slug TEXT NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    priority TEXT NOT NULL,
    kind TEXT NOT NULL,  -- "do" (actionable) or "fyi" (worth knowing)
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT,
    link TEXT,       -- where to act on it, if somewhere other than the hub
    link_text TEXT,
    PRIMARY KEY (rule, app_slug, key)
);

-- Google Play report files the hub has read, by their update time: a file is
-- read again only when Play Console rewrites it (the current month, daily).
CREATE TABLE IF NOT EXISTS play_files (
    name TEXT PRIMARY KEY,
    updated TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    packages TEXT  -- the Google Play apps it was read for
);

-- Totals from those files (see play_reports.py). Territory is "" for the whole
-- app, else a country code or Google's "Other". A rewritten file's rows are
-- replaced, never added to.
CREATE TABLE IF NOT EXISTS play_rows (
    file TEXT NOT NULL,
    package TEXT NOT NULL,
    date TEXT NOT NULL,
    territory TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (file, package, date, territory, metric)
);
CREATE INDEX IF NOT EXISTS play_rows_by_day ON play_rows (package, metric, date);

-- To-dos already posted to Discord, so each one posts once (see notify.py).
CREATE TABLE IF NOT EXISTS sent_alerts (
    key TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

-- Apple's App Crashes report (see crash_digest.py), by day, app version,
-- device and OS version. Like the other analytics files, a later file can
-- restate a day, so the newest file covering a day counts: apple_crashes_current.
-- Files are tracked in asc_instances under the report name "App Crashes".
CREATE TABLE IF NOT EXISTS apple_crash_rows (
    app_id TEXT NOT NULL,
    processing_date TEXT NOT NULL,
    date TEXT NOT NULL,
    app_version TEXT NOT NULL,
    device TEXT NOT NULL,
    platform_version TEXT NOT NULL,
    crashes REAL NOT NULL,
    devices REAL NOT NULL,
    PRIMARY KEY (app_id, processing_date, date, app_version, device, platform_version)
);

CREATE VIEW IF NOT EXISTS apple_crashes_current AS
SELECT r.* FROM apple_crash_rows r
JOIN (SELECT app_id, date, MAX(processing_date) AS newest FROM apple_crash_rows GROUP BY app_id, date) n
  ON r.app_id = n.app_id AND r.date = n.date AND r.processing_date = n.newest;

-- Weekly crash digests already posted, one per app and week, so a re-run
-- doesn't post the same week twice.
CREATE TABLE IF NOT EXISTS crash_digests (
    app TEXT NOT NULL,
    week_start TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (app, week_start)
);

-- What was set up in the app (see overlay.py). It wins over config.toml, the
-- environment and the secret files. Values are JSON.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Keys, tokens and webhooks saved in the app, encrypted (see crypto.py).
CREATE TABLE IF NOT EXISTS app_secrets (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- The owner's account: one row.
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL,
    totp_secret TEXT,                       -- encrypted
    totp_on INTEGER NOT NULL DEFAULT 0,
    totp_step INTEGER NOT NULL DEFAULT 0,   -- the last code used, so none works twice
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_codes (
    hash TEXT PRIMARY KEY,
    used_at TEXT
);

-- Signed-in browsers. Only a fingerprint of each cookie is kept. `pending` is a
-- right password still waiting for its two-step code.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    pending INTEGER NOT NULL DEFAULT 0,
    expires REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sign_in_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    issuer TEXT NOT NULL,
    client_id TEXT NOT NULL,
    client_secret TEXT NOT NULL,            -- encrypted
    enabled INTEGER NOT NULL DEFAULT 1
);

-- Single sign-ons on their way to the provider and back; each state works once.
CREATE TABLE IF NOT EXISTS oidc_states (
    state TEXT PRIMARY KEY,
    provider_id INTEGER NOT NULL,
    nonce TEXT NOT NULL,
    verifier TEXT NOT NULL,
    trying INTEGER NOT NULL DEFAULT 0,
    expires REAL NOT NULL
);
"""


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _add_columns(conn)
    return conn


def _add_columns(conn: sqlite3.Connection) -> None:
    """Columns added after the first release, for databases made before them."""
    for table, columns in (("hub_actions", ("link", "link_text")), ("play_files", ("packages",))):
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")  # noqa: S608 (fixed names)
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    """Save one in-app setting; None removes it, so the file or default applies again."""
    with conn:
        if value is None:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            conn.execute("INSERT OR REPLACE INTO app_settings VALUES (?, ?)", (key, json.dumps(value)))


def start_run(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute("INSERT INTO runs (source, started_at) VALUES (?, ?)", (source, now()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, ok: bool, message: str = "") -> None:
    conn.execute("UPDATE runs SET finished_at = ?, ok = ?, message = ? WHERE id = ?",
                 (now(), int(ok), message, run_id))
    conn.commit()


def last_runs(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per source: the latest finished run and the latest successful one."""
    out = {}
    for row in conn.execute("SELECT DISTINCT source FROM runs"):
        source = row["source"]
        last = conn.execute("SELECT * FROM runs WHERE source = ? AND finished_at IS NOT NULL "
                            "ORDER BY id DESC LIMIT 1", (source,)).fetchone()
        good = conn.execute("SELECT finished_at FROM runs WHERE source = ? AND ok = 1 "
                            "ORDER BY id DESC LIMIT 1", (source,)).fetchone()
        out[source] = {"last": dict(last) if last else None, "last_ok_at": good["finished_at"] if good else None}
    return out


def save_snapshot(conn: sqlite3.Connection, day: str, kind: str, key: str, body) -> None:
    conn.execute("INSERT OR REPLACE INTO asobeast_snapshots (day, kind, key, fetched_at, body) VALUES (?, ?, ?, ?, ?)",
                 (day, kind, key, now(), json.dumps(body)))


def latest_snapshot(conn: sqlite3.Connection, kind: str, key: str):
    row = conn.execute("SELECT body, day, fetched_at FROM asobeast_snapshots WHERE kind = ? AND key = ? "
                       "ORDER BY day DESC LIMIT 1", (kind, key)).fetchone()
    return (json.loads(row["body"]), row["fetched_at"]) if row else (None, None)


def daily_totals(conn: sqlite3.Connection, app_id: str, metric: str, territory: str | None = None,
                 uniques: bool = False, table: str = "asc_current") -> dict[str, float]:
    """Day -> total for one metric (from the analytics files, or `table="sales_rows"`),
    over every country unless `territory` is given."""
    # Both names come from this module, never from a request.
    column = "uniques" if uniques else "value"
    table = "sales_rows" if table == "sales_rows" else "asc_current"
    sql = f"SELECT date, SUM({column}) AS total FROM {table} WHERE app_id = ? AND metric = ?"  # noqa: S608
    params: list = [app_id, metric]
    if territory:
        sql += " AND territory = ?"
        params.append(territory.upper())
    sql += " GROUP BY date ORDER BY date"
    return {r["date"]: r["total"] or 0 for r in conn.execute(sql, params)}
