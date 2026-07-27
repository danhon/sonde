"""Settings, loaded from the environment (.env in local dev, compose env_file in prod)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Who we're tracking
    actor: str = field(default_factory=lambda: os.environ.get("BLUESKY_ACTOR", "danhon.com"))

    # API
    api_base: str = field(
        default_factory=lambda: os.environ.get("BLUESKY_API_BASE", "https://public.api.bsky.app")
    )
    # The 3000 req/5min cap is per IP and shared house-wide with BlueBirdNET and
    # atproto-labeler, so we deliberately sit well under it.
    rate_limit_per_second: float = field(
        default_factory=lambda: _float("API_RATE_LIMIT_PER_SECOND", 3.0)
    )
    http_timeout_seconds: float = field(default_factory=lambda: _float("HTTP_TIMEOUT_SECONDS", 30.0))
    max_retries: int = field(default_factory=lambda: _int("HTTP_MAX_RETRIES", 4))

    # Cadence
    head_sweep_minutes: int = field(default_factory=lambda: _int("HEAD_SWEEP_MINUTES", 15))
    # Hard ceiling on the head sweep. It normally stops after 1 page, but on an
    # empty database — or if the known-DID set is ever wrong — every page looks
    # new and it would walk all 115. At a 15-minute cadence that is ~11k calls a
    # day on a shared IP. The full sweep picks up whatever the cap leaves.
    head_sweep_max_pages: int = field(default_factory=lambda: _int("HEAD_SWEEP_MAX_PAGES", 10))
    full_sweep_hours: int = field(default_factory=lambda: _int("FULL_SWEEP_HOURS", 6))
    profile_ttl_days: int = field(default_factory=lambda: _int("PROFILE_TTL_DAYS", 7))

    # Affinity index. Sources come from a BAND of follow-list sizes: below the
    # floor a source endorses almost nobody (a pilot measured 0.8% coverage from
    # 200 such sources), above the ceiling the endorsement is diluted and the
    # list is expensive. Measured cost is ~7.2 calls per source.
    affinity_min_follows: int = field(default_factory=lambda: _int("AFFINITY_MIN_FOLLOWS", 150))
    affinity_max_follows: int = field(default_factory=lambda: _int("AFFINITY_MAX_FOLLOWS", 2000))
    affinity_max_sources: int = field(default_factory=lambda: _int("AFFINITY_MAX_SOURCES", 600))

    # Recent posts. getAuthorFeed is 1 call per actor with no bulk equivalent,
    # so fetching all 10,042 every run would be 40k calls/day on a shared IP.
    # Automatic post fetching covers the top N by influence plus every verified
    # follower. Everyone else is fetched on demand from their own page.
    posts_top_n: int = field(default_factory=lambda: _int("POSTS_TOP_N", 500))
    posts_per_run: int = field(default_factory=lambda: _int("POSTS_PER_RUN", 800))
    posts_ttl_hours: int = field(default_factory=lambda: _int("POSTS_TTL_HOURS", 20))

    # Exact relevance (getKnownFollowers) — auth only, 1+ calls per actor.
    relevance_top_n: int = field(default_factory=lambda: _int("RELEVANCE_TOP_N", 1000))

    # Curated moderation lists applied as a first-pass filter. Each list can
    # still be switched off individually in the UI.
    moderation_curators: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            h.strip() for h in
            os.environ.get("MODERATION_CURATORS", "skywatch.blue").split(",")
            if h.strip()
        )
    )
    moderation_enabled: bool = field(
        default_factory=lambda: _bool("MODERATION_ENABLED", True)
    )

    # Safety rails on departure detection
    departure_confirm_sweeps: int = field(
        default_factory=lambda: _int("DEPARTURE_CONFIRM_SWEEPS", 2)
    )
    mass_departure_pct: float = field(default_factory=lambda: _float("MASS_DEPARTURE_PCT", 2.0))

    # Display policy for followers who turned off logged-out visibility
    respect_no_unauthenticated: bool = field(
        default_factory=lambda: _bool("RESPECT_NO_UNAUTHENTICATED", False)
    )

    # Storage
    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "./sonde.db"))
    backup_dir: str = field(default_factory=lambda: os.environ.get("BACKUP_DIR", "./sonde-backups"))
    backup_keep: int = field(default_factory=lambda: _int("BACKUP_KEEP", 14))

    # Web
    web_host: str = field(default_factory=lambda: os.environ.get("WEB_HOST", "0.0.0.0"))
    web_port: int = field(default_factory=lambda: _int("WEB_PORT", 8090))

    # Build stamp, injected by the Dockerfile
    build_sha: str = field(default_factory=lambda: os.environ.get("BUILD_SHA", "dev"))
    build_time: str = field(default_factory=lambda: os.environ.get("BUILD_TIME", ""))

    @property
    def page_size(self) -> int:
        """getFollowers / getFollows max. Pages come back short; that is not an end signal."""
        return 100

    @property
    def profiles_batch(self) -> int:
        """actor.getProfiles hard maximum."""
        return 25

    def db_file(self) -> Path:
        return Path(self.db_path)


settings = Settings()
