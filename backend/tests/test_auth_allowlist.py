import pytest
from fastapi import HTTPException

from app.config import settings
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
