"""Where the hub finds its config, data and secrets.

Secrets are files outside the repo, named by environment variables, so a value
never sits in the environment or in a log. config.toml (in the repo) holds the
apps and rule thresholds.
"""
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class App:
    slug: str
    name: str
    store: str  # "app_store" or "google_play"
    markets: list[str]
    apple_id: str | None = None
    google_package: str | None = None

    @property
    def label(self) -> str:
        return f"{self.name} ({self.platform})"

    @property
    def platform(self) -> str:
        """How Discord posts and pages name the store: Apple covers iPhone, iPad and Apple TV."""
        return "Android" if self.store == "google_play" else "Apple"


@dataclass(frozen=True)
class Settings:
    apps: list[App]
    rules: dict
    keywords: dict
    database_path: Path
    crash_digest: dict = field(default_factory=dict)
    asc_issuer_id: str | None = None
    asc_key_id: str | None = None
    asc_key_path: Path | None = None
    asc_vendor_number: str | None = None
    asobeast_url: str = "http://asobeast-api:4000"
    asobeast_token_file: Path | None = None
    discord_webhook_file: Path | None = None
    google_key_path: Path | None = None
    play_bucket: str | None = None
    # The folder holding the #alerts webhooks: alerts-<app> per app (alerts-lumen)
    # and alerts-admin, the Admin-Stuff #alerts where failures go.
    alerts_webhook_dir: Path | None = None

    def app(self, slug: str) -> App | None:
        return next((a for a in self.apps if a.slug == slug), None)

    def alerts_webhook_file(self, name: str) -> Path | None:
        """The webhook file for an app's #alerts ("Lumen" -> alerts-lumen), or "admin"."""
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return self.alerts_webhook_dir / f"alerts-{slug}" if self.alerts_webhook_dir else None

    def read_secret(self, path: Path | None) -> str | None:
        if path is None or not path.exists():
            return None
        return path.read_text().strip() or None


def _path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def load(config_path: Path | None = None) -> Settings:
    config_path = config_path or _path("HUB_CONFIG") or ROOT / "config.toml"
    with open(config_path, "rb") as f:
        config = tomllib.load(f)
    apps = [App(**a) for a in config["apps"]]
    return Settings(
        apps=apps,
        rules=config.get("rules", {}),
        keywords=config.get("keywords", {}),
        crash_digest=config.get("crash_digest", {}),
        database_path=_path("DATABASE_PATH") or ROOT / "data" / "aso-hub.db",
        asc_issuer_id=os.environ.get("ASC_ISSUER_ID"),
        asc_key_id=os.environ.get("ASC_KEY_ID"),
        asc_key_path=_path("ASC_KEY_PATH"),
        asc_vendor_number=os.environ.get("ASC_VENDOR_NUMBER"),
        asobeast_url=os.environ.get("ASOBEAST_URL", "http://asobeast-api:4000").rstrip("/"),
        asobeast_token_file=_path("ASOBEAST_TOKEN_FILE"),
        discord_webhook_file=_path("DISCORD_WEBHOOK_FILE"),
        google_key_path=_path("GOOGLE_KEY_PATH"),
        play_bucket=os.environ.get("PLAY_BUCKET"),
        alerts_webhook_dir=_path("ALERTS_WEBHOOK_DIR"),
    )
