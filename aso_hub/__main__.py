"""python -m aso_hub serve | collect [asc|asobeast ...] | report-snapshot | crash-digest [--dry-run] [--again]"""
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

        uvicorn.run("aso_hub.web:app", host=os.environ.get("HOST", "127.0.0.1"),
                    port=int(os.environ.get("PORT", "8690")), proxy_headers=False)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
