import asyncio
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_HEADROOM_15MIN = 20
_HEADROOM_DAILY = 100
_WINDOW_15MIN_S = 900
_WINDOW_DAILY_S = 86400

# State file sits next to the SQLite DB (backend working directory).
_STATE_FILE = Path("rate_limit_state.json")


class BudgetExhausted(Exception):
    """Raised before an HTTP call when the proactive headroom is breached."""


class StravaRateLimiter:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._remaining_15min: int = 200
        self._remaining_daily: int = 2000
        self._reset_15min_at: float = time.monotonic() + _WINDOW_15MIN_S
        self._reset_daily_at: float = time.monotonic() + _WINDOW_DAILY_S
        self._load_state()

    # ── public interface ──────────────────────────────────────────────────────

    async def acquire(self) -> None:
        """Reserve one slot, or raise BudgetExhausted when headroom is gone."""
        async with self._lock:
            now = time.monotonic()
            if now >= self._reset_15min_at:
                self._remaining_15min = 200
                self._reset_15min_at = now + _WINDOW_15MIN_S
                logger.debug("15-min rate limit window reset")
            if now >= self._reset_daily_at:
                self._remaining_daily = 2000
                self._reset_daily_at = now + _WINDOW_DAILY_S
                logger.debug("daily rate limit window reset")

            if self._remaining_15min <= _HEADROOM_15MIN:
                raise BudgetExhausted(f"15-min headroom exhausted ({self._remaining_15min} remaining)")
            if self._remaining_daily <= _HEADROOM_DAILY:
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
            logger.debug(
                "strava budget: 15min=%d/%d  daily=%d/%d",
                self._remaining_15min, l15, self._remaining_daily, ldaily,
            )
            self._save_state()
        except (ValueError, AttributeError):
            pass

    def seconds_until_15min_reset(self) -> float:
        return max(0.0, self._reset_15min_at - time.monotonic())

    @property
    def remaining_15min(self) -> int:
        return self._remaining_15min

    @property
    def remaining_daily(self) -> int:
        return self._remaining_daily

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

            secs_until_15min = max(0.0, reset_15min_wall - wall_now)
            secs_until_daily = max(0.0, reset_daily_wall - wall_now)

            if secs_until_15min > 0:
                # Window hasn't reset yet — restore the saved remaining count.
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
