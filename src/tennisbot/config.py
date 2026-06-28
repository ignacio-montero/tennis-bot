"""Configuration loading: secrets from .env, targets from config/targets.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Project root = two levels up from this file (src/tennisbot/config.py -> root).
ROOT = Path(__file__).resolve().parents[2]

load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Secrets:
    ea_email: str
    ea_password: str
    telegram_bot_token: str
    telegram_chat_id: str

    @classmethod
    def from_env(cls) -> "Secrets":
        def req(key: str) -> str:
            val = os.environ.get(key, "").strip()
            if not val:
                raise RuntimeError(f"Missing required env var: {key} (check .env)")
            return val

        return cls(
            ea_email=req("EA_EMAIL"),
            ea_password=req("EA_PASSWORD"),
            telegram_bot_token=req("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=req("TELEGRAM_CHAT_ID"),
        )


@dataclass(frozen=True)
class Preference:
    day: str          # e.g. "Sat"
    time: str         # e.g. "18:00"


@dataclass(frozen=True)
class Surface:
    """A bookable court type, identified by matching the results-row name."""
    label: str        # short name used in config/notifications, e.g. "Synth"
    match: str        # substring of the Connect results-row name to match


@dataclass(frozen=True)
class CourtConfig:
    group: str                       # activity group code, e.g. "156COURTS"
    surfaces: dict[str, Surface]     # label -> Surface
    preferred: str                   # label of the preferred surface
    enabled: list[str]               # labels the bot may book (preference order)
    two_hours: bool = False          # book two consecutive hours, same court

    def ordered(self) -> list[Surface]:
        """Enabled surfaces, preferred first."""
        labels = [self.preferred] + [l for l in self.enabled if l != self.preferred]
        return [self.surfaces[l] for l in labels if l in self.surfaces]


@dataclass(frozen=True)
class ActivityItem:
    label: str
    match: str        # results-row name, e.g. "Tennis (adv) Sun 1300"
    day: str          # weekday the activity runs, e.g. "Sun"
    time: str         # start time, e.g. "13:00"


@dataclass(frozen=True)
class ActivityConfig:
    group: str                       # e.g. "156ADULT"
    items: dict[str, ActivityItem]   # label -> item
    enabled: list[str]

    def ordered(self) -> list[ActivityItem]:
        return [self.items[l] for l in self.enabled if l in self.items]


@dataclass(frozen=True)
class Drop:
    days_before: int
    local_time: str
    timezone: str


@dataclass(frozen=True)
class Target:
    key: str
    name: str
    provider: str
    site: str
    drop: Drop
    max_holds_per_run: int
    courts: CourtConfig | None = None
    activities: ActivityConfig | None = None
    want: list[Preference] = field(default_factory=list)


def _courts(raw: dict | None) -> CourtConfig | None:
    if not raw:
        return None
    surfaces = {s["label"]: Surface(s["label"], s["match"]) for s in raw["surfaces"]}
    return CourtConfig(
        group=str(raw["group"]),
        surfaces=surfaces,
        preferred=raw["preferred"],
        enabled=list(raw["enabled"]),
        two_hours=bool(raw.get("two_hours", False)),
    )


def _activities(raw: dict | None) -> ActivityConfig | None:
    if not raw:
        return None
    items = {i["label"]: ActivityItem(i["label"], i["match"], i["day"], str(i["time"]))
             for i in raw["items"]}
    return ActivityConfig(group=str(raw["group"]), items=items,
                          enabled=list(raw["enabled"]))


def load_targets(path: Path | None = None) -> dict[str, Target]:
    path = path or (ROOT / "config" / "targets.yaml")
    raw = yaml.safe_load(path.read_text())
    targets: dict[str, Target] = {}
    for key, t in raw.items():
        d = t["drop"]
        targets[key] = Target(
            key=key,
            name=t["name"],
            provider=t["provider"],
            site=str(t["site"]),
            drop=Drop(d["days_before"], str(d["local_time"]), d["timezone"]),
            max_holds_per_run=int(t.get("max_holds_per_run", 1)),
            courts=_courts(t.get("courts")),
            activities=_activities(t.get("activities")),
            want=[Preference(p["day"], str(p["time"])) for p in t.get("want", [])],
        )
    return targets
