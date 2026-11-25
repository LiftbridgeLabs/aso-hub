"""The hub's own clock: the data pull and the weekly crash digest, at the times set
under Settings → Schedule. Runs inside `serve`, one job at a time.
"""
import datetime as dt
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import collect, crash_digest, db, overlay, settings

log = logging.getLogger("aso_hub.scheduler")
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
TICK_SECONDS = 30
RUNNING = threading.Lock()  # one pull or digest at a time


def schedule(conn: sqlite3.Connection) -> dict:
    default = {"enabled": True, "timezone": "UTC", "pull_times": ["07:00", "13:00"], "digest_day": 0,
               "digest_time": "08:00"}
    return default | db.get_setting(conn, "schedule", {})


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _at(day: dt.date, time: str, tz: ZoneInfo) -> dt.datetime:
    hour, minute = map(int, time.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tz)


def slots(plan: dict, job: str, now: dt.datetime) -> tuple[dt.datetime | None, dt.datetime | None]:
    """The job's latest time that has come, and its next one."""
    tz = zone(plan["timezone"])
    today = now.astimezone(tz).date()
    times = plan["pull_times"] if job == "pull" else [plan["digest_time"]]
    found = [_at(today + dt.timedelta(days=offset), time, tz)
             for offset in range(-8, 9) for time in times
             if job == "pull" or (today + dt.timedelta(days=offset)).weekday() == plan["digest_day"]]
    past = [s for s in found if s <= now]
    later = [s for s in found if s > now]
    return (max(past) if past else None), (min(later) if later else None)


def due(conn: sqlite3.Connection, now: dt.datetime) -> Iterator[str]:
    """The jobs whose time has come since they last ran, one at a time. Each is marked
    taken only as it is handed out: a failing job waits for its next time, and one not
    reached before a restart is still due after it."""
    for job in ("pull", "digest"):
        plan = schedule(conn)
        if not plan["enabled"]:
            return
        latest, _ = slots(plan, job, now)
        if latest is None:
            continue
        mark = latest.astimezone(dt.UTC).isoformat()
        done = db.get_setting(conn, "schedule_done", {})
        if job in done and mark <= done[job]:
            continue
        first = job not in done
        db.set_setting(conn, "schedule_done", done | {job: mark})
        # A new hub pulls straight away; the digest waits for its day.
        if not (first and job == "digest"):
            yield job


def run_job(job: str) -> None:
    """Run one job now. The caller holds RUNNING."""
    base = settings.load()
    conn = db.connect(base.database_path)
    try:
        cfg = overlay.effective(base, conn)
        if job == "pull":
            if cfg.apps or cfg.stored_secrets or cfg.read_secret(cfg.asobeast_token_file):
                collect.run(base, conn)
        elif cfg.crash_digest.get("apps"):
            crash_digest.run(base, conn)
    finally:
        conn.close()


def run_now(job: str) -> bool:
    """Start a job in the background; False when one is already running."""
    if not RUNNING.acquire(blocking=False):
        return False

    def work() -> None:
        try:
            run_job(job)
        except Exception:
            log.exception("the %s failed", job)
        finally:
            RUNNING.release()

    threading.Thread(target=work, name=f"hub-{job}", daemon=True).start()
    return True


def tick(now: dt.datetime | None = None) -> None:
    base = settings.load()
    if not RUNNING.acquire(blocking=False):
        return  # a job is running; whatever is due is picked up afterwards
    try:
        conn = db.connect(base.database_path)
        try:
            for job in due(conn, now or dt.datetime.now(dt.UTC)):
                try:
                    run_job(job)
                except Exception:
                    log.exception("the scheduled %s failed", job)
        finally:
            conn.close()
    finally:
        RUNNING.release()


def start(stop: threading.Event) -> threading.Thread:
    def loop() -> None:
        while not stop.is_set():
            try:
                tick()
            except Exception:
                log.exception("the scheduler couldn't check what is due")
            stop.wait(TICK_SECONDS)

    thread = threading.Thread(target=loop, name="hub-scheduler", daemon=True)
    thread.start()
    return thread
