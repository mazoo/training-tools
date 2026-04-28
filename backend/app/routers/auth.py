from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.models.athlete import AthleteProfile, AthleteToken
from app.services.auth import create_session_token, get_current_athlete_id
from app.services.permissions import get_authorization_state, grant_initial_admin_if_needed
from app.strava.client import StravaClient, StravaRateLimitError

router = APIRouter(tags=["auth"])

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
SCOPES = "read,activity:read,profile:read_all"
_CALLBACK_PATH = urlparse(settings.strava_redirect_uri).path


@router.get("/auth/login")
async def login() -> RedirectResponse:
    url = (
        f"{STRAVA_AUTH_URL}"
        f"?client_id={settings.strava_client_id}"
        f"&redirect_uri={settings.strava_redirect_uri}"
        f"&response_type=code"
        f"&scope={SCOPES}"
        f"&approval_prompt=auto"
    )
    return RedirectResponse(url=url)


@router.get(_CALLBACK_PATH)
async def callback(
    code: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    if error or not code:
        raise HTTPException(status_code=400, detail=error or "Missing code")

    token_data = await StravaClient.exchange_token(code)
    athlete_id = token_data["athlete"]["id"]

    await db.execute(
        sqlite_insert(AthleteToken)
        .values(
            athlete_id=athlete_id,
            access_token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            expires_at=token_data["expires_at"],
        )
        .on_conflict_do_update(
            index_elements=["athlete_id"],
            set_={
                "access_token": token_data["access_token"],
                "refresh_token": token_data["refresh_token"],
                "expires_at": token_data["expires_at"],
            },
        )
    )

    # Athlete profile is included in the token exchange response — store it
    # here so /api/auth/me never needs a separate Strava API call.
    athlete = token_data.get("athlete", {})
    await db.execute(
        sqlite_insert(AthleteProfile)
        .values(
            athlete_id=athlete_id,
            firstname=athlete.get("firstname"),
            lastname=athlete.get("lastname"),
            profile_medium=athlete.get("profile_medium"),
        )
        .on_conflict_do_update(
            index_elements=["athlete_id"],
            set_={
                "firstname": athlete.get("firstname"),
                "lastname": athlete.get("lastname"),
                "profile_medium": athlete.get("profile_medium"),
            },
        )
    )

    await grant_initial_admin_if_needed(db, athlete_id)

    session_token = create_session_token(athlete_id)
    return RedirectResponse(url=f"{settings.frontend_url}/?token={session_token}")


@router.get("/api/auth/me")
async def me(
    athlete_id: int = Depends(get_current_athlete_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(AthleteProfile).where(AthleteProfile.athlete_id == athlete_id)
    )
    profile = result.scalar_one_or_none()
    authz = await get_authorization_state(db, athlete_id)

    if profile:
        return {
            "athlete_id": athlete_id,
            "firstname": profile.firstname,
            "lastname": profile.lastname,
            "profile": profile.profile_medium,
            "home_address": profile.home_address,
            "home_lat": profile.home_lat,
            "home_lng": profile.home_lng,
            "roles": authz.roles,
            "permissions": authz.permissions,
        }

    # Fallback for accounts connected before profile caching was added.
    # Fetches from Strava once, then stores so this branch is never hit again.
    from app.services.auth import get_valid_access_token
    try:
        access_token = await get_valid_access_token(athlete_id, db)
        athlete = await StravaClient(access_token).get_athlete()
    except StravaRateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail={"strava_error": "Rate limit reached", "retry_after_s": exc.retry_after_s},
        )
    await db.execute(
        sqlite_insert(AthleteProfile)
        .values(
            athlete_id=athlete_id,
            firstname=athlete.get("firstname"),
            lastname=athlete.get("lastname"),
            profile_medium=athlete.get("profile_medium"),
        )
        .on_conflict_do_update(
            index_elements=["athlete_id"],
            set_={
                "firstname": athlete.get("firstname"),
                "lastname": athlete.get("lastname"),
                "profile_medium": athlete.get("profile_medium"),
            },
        )
    )
    await db.commit()
    authz = await get_authorization_state(db, athlete_id)
    return {
        "athlete_id": athlete_id,
        "firstname": athlete.get("firstname"),
        "lastname": athlete.get("lastname"),
        "profile": athlete.get("profile_medium"),
        "home_address": None,
        "home_lat": None,
        "home_lng": None,
        "roles": authz.roles,
        "permissions": authz.permissions,
    }
