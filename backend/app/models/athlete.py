import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
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
    sex: Mapped[str | None] = mapped_column(String, nullable=True)
    home_address: Mapped[str | None] = mapped_column(String, nullable=True)
    home_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_lng: Mapped[float | None] = mapped_column(Float, nullable=True)


class AthleteSyncState(Base):
    __tablename__ = "athlete_sync_state"

    athlete_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bootstrap_done: Mapped[bool] = mapped_column(Boolean, default=False)
    last_activity_sync_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_star_sync_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # Oldest timestamp covered by historical backfill; None = initial sync not yet done.
    # Segment-effort backfill marks per-segment progress separately.
    backfill_cursor_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    backfill_complete: Mapped[bool] = mapped_column(Boolean, default=False)


class AthleteZones(Base):
    __tablename__ = "athlete_zones"

    athlete_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zones_json: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    )


class AthleteRole(Base):
    __tablename__ = "athlete_roles"

    athlete_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
