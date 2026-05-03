import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.athlete import AthleteProfile
from app.routers import auth


def test_empty_allowlist_allows_any_athlete(monkeypatch):
    monkeypatch.setattr(settings, "allowed_athlete_ids", "")

    auth._require_allowed_athlete(123)


def test_allowlist_accepts_comma_separated_athlete_ids(monkeypatch):
    monkeypatch.setattr(settings, "allowed_athlete_ids", "123, 456")

    auth._require_allowed_athlete(456)


def test_allowlist_rejects_unknown_athlete(monkeypatch):
    monkeypatch.setattr(settings, "allowed_athlete_ids", "123,456")

    with pytest.raises(HTTPException) as exc:
        auth._require_allowed_athlete(789)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_callback_rejects_disallowed_athlete_before_database_write(monkeypatch):
    monkeypatch.setattr(settings, "allowed_athlete_ids", "456")

    async def fake_exchange_token(code: str) -> dict:
        assert code == "oauth-code"
        return {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 9999999999,
            "athlete": {"id": 123, "firstname": "Private", "lastname": "Athlete"},
        }

    class FailingSession:
        async def execute(self, *args, **kwargs):
            raise AssertionError("disallowed athlete should not be stored")

    monkeypatch.setattr(auth.StravaClient, "exchange_token", staticmethod(fake_exchange_token))

    with pytest.raises(HTTPException) as exc:
        await auth.callback(code="oauth-code", db=FailingSession())

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_callback_caches_athlete_sex_from_token_exchange(monkeypatch):
    monkeypatch.setattr(settings, "allowed_athlete_ids", "")

    async def fake_exchange_token(code: str) -> dict:
        assert code == "oauth-code"
        return {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 9999999999,
            "athlete": {
                "id": 123,
                "firstname": "Fast",
                "lastname": "Rider",
                "profile_medium": "https://example.test/avatar.jpg",
                "sex": "F",
            },
        }

    monkeypatch.setattr(auth.StravaClient, "exchange_token", staticmethod(fake_exchange_token))

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        response = await auth.callback(code="oauth-code", db=session)
        assert response.status_code == 307

        result = await session.execute(
            select(AthleteProfile).where(AthleteProfile.athlete_id == 123)
        )
        profile = result.scalar_one()
        assert profile.firstname == "Fast"
        assert profile.lastname == "Rider"
        assert profile.profile_medium == "https://example.test/avatar.jpg"
        assert profile.sex == "F"

        current_athlete = await auth.me(athlete_id=123, db=session)
        assert current_athlete["sex"] == "F"

    await engine.dispose()
