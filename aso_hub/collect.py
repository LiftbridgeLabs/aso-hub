"""The scheduled pull: read every source into the database, then check the rules.

One source failing doesn't stop the others; each run is recorded so the pages
can say what is fresh and what isn't.
"""
import sqlite3
import sys
import traceback

import httpx

from . import asc, asobeast, db, notify, play, rules
from .settings import Settings

SOURCES = ("asc", "asobeast", "play")


def _pull_asc(settings: Settings, conn: sqlite3.Connection) -> str:
    key = settings.read_secret(settings.asc_key_path)
    if not (settings.asc_issuer_id and settings.asc_key_id and key and settings.asc_vendor_number):
        raise asc.AscError("App Store Connect isn't set up "
                           "(ASC_ISSUER_ID, ASC_KEY_ID, ASC_KEY_PATH, ASC_VENDOR_NUMBER).")
    client = asc.Client(settings.asc_issuer_id, settings.asc_key_id, key)
    days = asc.pull_sales(conn, client, settings.asc_vendor_number)
    files, waiting, failed = 0, [], []
    for app in settings.apps:
        if app.store == "app_store":
            try:
                files += asc.pull_app(conn, client, app.apple_id)
            except asc.NoReportRequest:
                # A new app: its downloads still come from the sales reports above.
                waiting.append(app.label)
            except asc.AscError as e:
                failed.append(f"{app.label}: {e}")
            except httpx.HTTPError as e:
                # Network errors can carry a report file's presigned link; keep only the kind.
                failed.append(f"{app.label}: couldn't reach Apple ({type(e).__name__})")
    if failed:
        # Raised after the loop, so one app's problem doesn't stop the apps after it.
        raise asc.AscError("; ".join(failed))
    message = f"read {days} sales days and {files} analytics files"
    return message + (f"; no store page reports requested yet: {', '.join(waiting)}" if waiting else "")


def _pull_asobeast(settings: Settings, conn: sqlite3.Connection) -> str:
    return asobeast.pull(conn, settings)


def _pull_play(settings: Settings, conn: sqlite3.Connection) -> str:
    return play.pull(conn, settings)


PULLS = {"asc": _pull_asc, "asobeast": _pull_asobeast, "play": _pull_play}
KNOWN_ERRORS = (asc.AscError, asobeast.AsoBeastError, play.PlayError)


def run(settings: Settings, conn: sqlite3.Connection, sources=SOURCES) -> bool:
    all_ok = True
    for source in sources:
        run_id = db.start_run(conn, source)
        try:
            message = PULLS[source](settings, conn)
            db.finish_run(conn, run_id, True, message)
            print(f"{source}: {message}")
        except KNOWN_ERRORS as e:
            all_ok = False
            db.finish_run(conn, run_id, False, str(e))
            print(f"{source} failed: {e}", file=sys.stderr)
        except httpx.HTTPError as e:
            # Network errors can carry a report file's presigned link; keep only the kind.
            all_ok = False
            db.finish_run(conn, run_id, False, f"Couldn't reach {source} ({type(e).__name__}).")
            print(f"{source} failed: {type(e).__name__}", file=sys.stderr)
        except Exception as e:
            all_ok = False
            db.finish_run(conn, run_id, False, f"Unexpected error: {type(e).__name__}. See the hub's log.")
            traceback.print_exc()
    rules.evaluate(settings, conn)
    all_ok = _notify(settings, conn) and all_ok
    return all_ok


def _notify(settings: Settings, conn: sqlite3.Connection) -> bool:
    """Post new to-dos. A failed post is retried on the next pull, since nothing is marked sent."""
    run_id = db.start_run(conn, "discord")
    try:
        message = notify.send(conn, settings)
    except notify.NotifyError as e:
        db.finish_run(conn, run_id, False, str(e))
        print(f"discord failed: {e}", file=sys.stderr)
        return False
    db.finish_run(conn, run_id, True, message)
    print(f"discord: {message}")
    return True
