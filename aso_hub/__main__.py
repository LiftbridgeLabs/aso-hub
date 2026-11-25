"""python -m aso_hub serve | collect [asc|asobeast ...] | report-snapshot | crash-digest [--dry-run] [--again]
| reset-password | turn-off-two-step"""
import getpass
import os
import sys

from . import collect, db, settings


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "serve"
    cfg = settings.load()
    if command == "collect":
        sources = argv[1:] or list(collect.SOURCES)
        unknown = [s for s in sources if s not in collect.SOURCES]
        if unknown:
            print(f"unknown source: {', '.join(unknown)}", file=sys.stderr)
            return 2
        return 0 if collect.run(cfg, db.connect(cfg.database_path), sources) else 1
    if command == "report-snapshot":
        import json

        from . import daily_report

        print(json.dumps(daily_report.snapshot(db.connect(cfg.database_path), cfg), indent=1))
        return 0
    if command == "crash-digest":
        from . import crash_digest

        flags = set(argv[1:])
        if flags - {"--dry-run", "--again"}:
            print(__doc__, file=sys.stderr)
            return 2
        # 3: a failure the digest already reported in #alerts, which the timer
        # doesn't retry (anything else, like the container being down, it does).
        return 0 if crash_digest.run(cfg, db.connect(cfg.database_path), dry_run="--dry-run" in flags,
                                     again="--again" in flags) else 3
    if command == "serve":
        import uvicorn

        os.environ["ASO_HUB_SERVING"] = "1"
        uvicorn.run("aso_hub.web:app", host=os.environ.get("HOST", "127.0.0.1"),
                    port=int(os.environ.get("PORT", "8690")), proxy_headers=False)
        return 0
    if command in ("reset-password", "turn-off-two-step"):
        return account_command(cfg, command)
    print(__doc__, file=sys.stderr)
    return 2


def account_command(cfg: settings.Settings, command: str) -> int:
    """For whoever runs the server: when the password or the phone is lost."""
    from . import auth

    conn = db.connect(cfg.database_path)
    if command == "turn-off-two-step":
        auth.turn_off_two_step(conn)
        auth.end_other_sessions(conn, None)
        print("Two-step sign-in is off, and every browser is signed out.")
        return 0
    try:
        password = getpass.getpass("New password: ")
        auth.check_new_password(password, getpass.getpass("Again: "))
        auth.set_password(conn, password)
    except (auth.AuthError, EOFError) as e:
        print(str(e) or "no password typed", file=sys.stderr)
        return 1
    print("Password saved, and every browser is signed out.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
