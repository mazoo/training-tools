import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AthleteToken(Base):
    __tablename__ = "athlete_tokens"

    athlete_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    access_token: Mapped[str] = mapped_column(String, nullable=False)
    refresh_token: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)


class AthleteProfile(Base):
    __tablename__ = "athlete_profile"

    athlete_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firstname: Mapped[str | None] = mapped_column(String, nullable=True)
    lastname: Mapped[str | None] = mapped_column(String, nullable=True)
    profile_medium: Mapped[str | None] = mapped_column(String, nullable=True)
    home_address: Mapped[str | None] = mapped_column(String, nullable=True)
    home_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_lng: Mapped[float | None] = mapped_column(Float, nullable=True)


class AthleteSyncState(Base):
    __tablename__ = "athlete_sync_state"

    athlete_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bootstrap_done: Mapped[bool] = mapped_column(Boolean, default=False)
    last_activity_sync_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_star_sync_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
