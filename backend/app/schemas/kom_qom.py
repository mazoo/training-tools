from datetime import date, datetime

from pydantic import BaseModel


class CandidateFilters(BaseModel):
    effort_time_min: int | None = None
    effort_time_max: int | None = None
    gradient_min: float | None = None
    gradient_max: float | None = None
    surface: str = "all"  # all | outdoor | indoor
    podium_only: bool = False


class SegmentCandidate(BaseModel):
    segment_id: int
    segment_name: str
    top10_seen: bool
    podium_seen: bool
    best_seen_kom_rank: int | None
    last_seen_kom_rank: int | None
    # is_kom: athlete currently holds KOM per Strava starred response
    is_kom: bool
    # data_quality distinguishes PR-seeded first-load rows from imported effort history.
    data_quality: str
    best_time_s: int | None
    best_time_display: str | None
    latest_time_s: int | None
    latest_time_display: str | None
    # pr_time_s: Strava's authoritative all-time PR (may predate our sync window)
    pr_time_s: int | None
    pr_time_display: str | None
    pr_date: datetime | None
    times_ridden: int
    best_avg_watts: float | None
    latest_avg_watts: float | None
    last_ridden_at: datetime | None
    starred_date: datetime | None
    kom_time_s: int | None
    kom_time_display: str | None
    gap_to_kom_s: int | None
    gap_to_kom_display: str | None
    gap_to_kom_pct: float | None
    average_grade: float | None
    distance_m: float | None
    elevation_high: float | None
    elevation_low: float | None
    distance_from_home_km: float | None
    is_indoor: bool
    activity_type: str | None
    hazardous: bool | None
    city: str | None
    state: str | None
    country: str | None
    climb_category: int | None
    segment_url: str


class CandidatesResponse(BaseModel):
    fetched_at: datetime
    total: int
    candidates: list[SegmentCandidate]


class RefreshResponse(BaseModel):
    task_id: str
    message: str


class RefreshStatusResponse(BaseModel):
    status: str
    activities_processed: int
    activities_total: int
    strava_calls_made: int
    strava_budget_remaining_15min: int
    strava_budget_remaining_daily: int
    error: str | None = None
    retry_after: datetime | None = None


class BackfillAvailabilityResponse(BaseModel):
    has_permission: bool
    available: bool
    reason: str | None = None
    strava_budget_remaining_15min: int
    strava_budget_remaining_daily: int
    retry_after_seconds: int | None = None
