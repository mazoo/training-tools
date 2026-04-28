import time

from fastapi import Header, HTTPException
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.athlete import AthleteToken
from app.strava.client import StravaClient

_signer = URLSafeSerializer(settings.secret_key, salt="session")


def create_session_token(athlete_id: int) -> str:
    return _signer.dumps(athlete_id)


def decode_session_token(token: str) -> int | None:
    try:
        return _signer.loads(token)
    except BadSignature:
        return None


async def get_current_athlete_id(authorization: str | None = Header(default=None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.removeprefix("Bearer ")
    athlete_id = decode_session_token(token)
    if athlete_id is None:
        raise HTTPException(status_code=401, detail="Invalid session token")
    return athlete_id


async def get_valid_access_token(athlete_id: int, db: AsyncSession) -> str:
    result = await db.execute(
        select(AthleteToken).where(AthleteToken.athlete_id == athlete_id)
    )
    token_row = result.scalar_one_or_none()
    if not token_row:
        raise HTTPException(status_code=401, detail="No Strava token found — please reconnect")

    if time.time() + 60 > token_row.expires_at:
        refreshed = await StravaClient.refresh_access_token(token_row.refresh_token)
        token_row.access_token = refreshed["access_token"]
        token_row.refresh_token = refreshed["refresh_token"]
        token_row.expires_at = refreshed["expires_at"]
        await db.commit()

    return token_row.access_token
