import asyncio
import json
import logging
import time
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

HEADROOM_15MIN = 20
HEADROOM_DAILY = 100
_WINDOW_15MIN_S = 900
_WINDOW_DAILY_S = 86400

# State file defaults to the backend working directory in dev. Production should
# point this at the same persistent data volume as SQLite.
_STATE_FILE = Path(settings.rate_limit_state_path)


class BudgetExhausted(Exception):
    """Raised before an HTTP call when the proactive headroom is breached."""


class StravaRateLimiter:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._remaining_15min: int = 200
        self._remaining_daily: int = 2000
        self._reset_15min_at: float = self._next_monotonic_boundary(_WINDOW_15MIN_S)
        self._reset_daily_at: float = self._next_monotonic_boundary(_WINDOW_DAILY_S)
        self._load_state()

    # ── public interface ──────────────────────────────────────────────────────

    async def acquire(self) -> None:
        """Reserve one slot, or raise BudgetExhausted when headroom is gone."""
        async with self._lock:
            self._refresh_windows()

            if self._remaining_15min <= HEADROOM_15MIN:
                raise BudgetExhausted(f"15-min headroom exhausted ({self._remaining_15min} remaining)")
            if self._remaining_daily <= HEADROOM_DAILY:
                raise BudgetExhausted(f"daily headroom exhausted ({self._remaining_daily} remaining)")

            self._remaining_15min -= 1
            self._remaining_daily -= 1

    def sync_from_headers(self, headers: dict) -> None:
        """Overwrite remaining budget from Strava response headers (ground truth), then persist."""
        usage_raw = headers.get("x-ratelimit-usage", "")
        limit_raw = headers.get("x-ratelimit-limit", "")
        if not usage_raw or not limit_raw:
            return
        try:
            u15, udaily = map(int, usage_raw.split(","))
            l15, ldaily = map(int, limit_raw.split(","))
            self._remaining_15min = max(0, l15 - u15)
            self._remaining_daily = max(0, ldaily - udaily)
            self._reset_15min_at = self._next_monotonic_boundary(_WINDOW_15MIN_S)
            self._reset_daily_at = self._next_monotonic_boundary(_WINDOW_DAILY_S)
            logger.debug(
                "strava budget: 15min=%d/%d  daily=%d/%d",
                self._remaining_15min, l15, self._remaining_daily, ldaily,
            )
            self._save_state()
        except (ValueError, AttributeError):
            pass

    def seconds_until_15min_reset(self) -> float:
        self._refresh_windows()
        return max(0.0, self._reset_15min_at - time.monotonic())

    def seconds_until_daily_reset(self) -> float:
        self._refresh_windows()
        return max(0.0, self._reset_daily_at - time.monotonic())

    @property
    def remaining_15min(self) -> int:
        self._refresh_windows()
        return self._remaining_15min

    @property
    def remaining_daily(self) -> int:
        self._refresh_windows()
        return self._remaining_daily

    def _refresh_windows(self) -> None:
        now = time.monotonic()
        changed = False
        if now >= self._reset_15min_at:
            self._remaining_15min = 200
            self._reset_15min_at = self._next_monotonic_boundary(_WINDOW_15MIN_S)
            changed = True
            logger.debug("15-min rate limit window reset")
        if now >= self._reset_daily_at:
            self._remaining_daily = 2000
            self._reset_daily_at = self._next_monotonic_boundary(_WINDOW_DAILY_S)
            changed = True
            logger.debug("daily rate limit window reset")
        if changed:
            self._save_state()

    @staticmethod
    def _next_monotonic_boundary(window_s: int) -> float:
        seconds_until_reset = StravaRateLimiter._seconds_until_next_boundary(window_s)
        return time.monotonic() + seconds_until_reset

    @staticmethod
    def _seconds_until_next_boundary(window_s: int, wall_now: float | None = None) -> float:
        wall_now = time.time() if wall_now is None else wall_now
        seconds_until_reset = window_s - (wall_now % window_s)
        if seconds_until_reset <= 0:
            seconds_until_reset = window_s
        return seconds_until_reset

    # ── persistence ───────────────────────────────────────────────────────────

    def _save_state(self) -> None:
        try:
            wall_now = time.time()
            mono_now = time.monotonic()
            _STATE_FILE.write_text(json.dumps({
                "remaining_15min": self._remaining_15min,
                "remaining_daily": self._remaining_daily,
                # Store as wall-clock times so they survive process restarts.
                "reset_15min_wall": wall_now + (self._reset_15min_at - mono_now),
                "reset_daily_wall": wall_now + (self._reset_daily_at - mono_now),
            }))
        except OSError:
            pass

    def _load_state(self) -> None:
        try:
            data = json.loads(_STATE_FILE.read_text())
            wall_now = time.time()
            mono_now = time.monotonic()

            reset_15min_wall = data.get("reset_15min_wall", wall_now + _WINDOW_15MIN_S)
            reset_daily_wall = data.get("reset_daily_wall", wall_now + _WINDOW_DAILY_S)

            secs_until_15min = self._seconds_until_next_boundary(_WINDOW_15MIN_S, wall_now)
            secs_until_daily = max(0.0, reset_daily_wall - wall_now)

            current_15min_window_started_at = wall_now - (wall_now % _WINDOW_15MIN_S)
            if reset_15min_wall > current_15min_window_started_at:
                # Restore saved budget from the current Strava window, but align
                # the retry time to Strava's fixed quarter-hour boundary.
                self._remaining_15min = data.get("remaining_15min", 200)
                self._reset_15min_at = mono_now + secs_until_15min
            # else: window already elapsed, keep default 200 (full budget)

            if secs_until_daily > 0:
                self._remaining_daily = data.get("remaining_daily", 2000)
                self._reset_daily_at = mono_now + secs_until_daily

            logger.info(
                "rate limiter restored: 15min=%d (resets in %.0fs)  daily=%d",
                self._remaining_15min, secs_until_15min, self._remaining_daily,
            )
        except (OSError, KeyError, json.JSONDecodeError):
            pass  # no state file yet, start fresh


rate_limiter = StravaRateLimiter()
