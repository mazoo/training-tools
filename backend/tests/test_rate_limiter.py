import json

import pytest

from app.strava import rate_limiter as rl


def test_sync_from_headers_aligns_15min_reset_to_strava_window(monkeypatch, tmp_path):
    monkeypatch.setattr(rl, "_STATE_FILE", tmp_path / "rate-limit.json")
    monkeypatch.setattr(rl.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(rl.time, "monotonic", lambda: 5_000.0)

    limiter = rl.StravaRateLimiter()
    limiter.sync_from_headers({
        "x-ratelimit-usage": "200,100",
        "x-ratelimit-limit": "200,2000",
    })

    assert limiter.remaining_15min == 0
    assert limiter.seconds_until_15min_reset() == pytest.approx(800.0)


def test_saved_15min_state_is_restored_until_strava_window_boundary(monkeypatch, tmp_path):
    state_file = tmp_path / "rate-limit.json"
    state_file.write_text(json.dumps({
        "remaining_15min": 0,
        "remaining_daily": 1900,
        "reset_15min_wall": 1_700.0,
        "reset_daily_wall": 2_000.0,
    }))
    monkeypatch.setattr(rl, "_STATE_FILE", state_file)
    monkeypatch.setattr(rl.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(rl.time, "monotonic", lambda: 5_000.0)

    limiter = rl.StravaRateLimiter()

    assert limiter.remaining_15min == 0
    assert limiter.seconds_until_15min_reset() == pytest.approx(800.0)


def test_old_saved_15min_reset_inside_current_window_stays_limited(monkeypatch, tmp_path):
    state_file = tmp_path / "rate-limit.json"
    state_file.write_text(json.dumps({
        "remaining_15min": 0,
        "remaining_daily": 1900,
        "reset_15min_wall": 1_700.0,
        "reset_daily_wall": 2_000.0,
    }))
    monkeypatch.setattr(rl, "_STATE_FILE", state_file)
    monkeypatch.setattr(rl.time, "time", lambda: 1_701.0)
    monkeypatch.setattr(rl.time, "monotonic", lambda: 5_000.0)

    limiter = rl.StravaRateLimiter()

    assert limiter.remaining_15min == 0
    assert limiter.seconds_until_15min_reset() == pytest.approx(99.0)
