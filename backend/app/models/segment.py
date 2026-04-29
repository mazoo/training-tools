import datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SegmentEffortDigest(Base):
    __tablename__ = "segment_effort_digest"

    effort_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    segment_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    activity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effort_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    elapsed_s: Mapped[int] = mapped_column(Integer, nullable=False)
    moving_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    kom_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AthleteSegmentProfile(Base):
    __tablename__ = "athlete_segment_profile"

    athlete_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    segment_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    segment_name: Mapped[str] = mapped_column(String, nullable=False)
    is_starred: Mapped[bool] = mapped_column(Boolean, default=False)
    is_indoor: Mapped[bool] = mapped_column(Boolean, default=False)
    times_ridden: Mapped[int] = mapped_column(Integer, default=0)
    best_time_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_time_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_avg_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    latest_avg_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    top10_seen: Mapped[bool] = mapped_column(Boolean, default=False)
    podium_seen: Mapped[bool] = mapped_column(Boolean, default=False)
    best_seen_kom_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_kom_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_ridden_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # Populated from athlete_pr_effort in GET /segments/starred response
    is_kom: Mapped[bool] = mapped_column(Boolean, default=False)
    pr_time_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_activity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pr_date: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    starred_date: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)


class SegmentEnrichment(Base):
    __tablename__ = "segment_enrichment"

    segment_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    segment_name: Mapped[str | None] = mapped_column(String, nullable=True)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_grade_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_grade_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    city: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)
    climb_category: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elevation_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    elevation_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    activity_type: Mapped[str | None] = mapped_column(String, nullable=True)
    hazardous: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    kom_time_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kom_time_checked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    cached_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)


class SegmentEffortBackfillState(Base):
    __tablename__ = "segment_effort_backfill_state"

    athlete_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    segment_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_attempt_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
