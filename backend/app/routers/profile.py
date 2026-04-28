from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.athlete import AthleteProfile, AthleteRole, AthleteSyncState, AthleteToken
from app.models.segment import (
    AthleteSegmentProfile,
    SegmentEffortBackfillState,
    SegmentEffortDigest,
)
from app.services.auth import get_current_athlete_id

router = APIRouter(prefix="/api/profile", tags=["profile"])

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "training-tools/1.0 (personal cycling app)"

CurrentAthlete = Annotated[int, Depends(get_current_athlete_id)]
DB = Annotated[AsyncSession, Depends(get_db)]


async def _geocode(address: str) -> tuple[float, float]:
    params = {"format": "jsonv2", "limit": "1", "q": address}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                _NOMINATIM_URL,
                params=params,
                headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Geocoding service unavailable")

    results = resp.json()
    if not results:
        raise HTTPException(status_code=422, detail="Address not found — try being more specific")

    entry = results[0]
    try:
        return float(entry["lat"]), float(entry["lon"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=502, detail="Unexpected geocoder response")


class HomeAddressUpdate(BaseModel):
    address: str


@router.put(
    "/home",
    responses={422: {"description": "Empty or unresolvable address"}, 502: {"description": "Geocoding service error"}},
)
async def update_home(body: HomeAddressUpdate, athlete_id: CurrentAthlete, db: DB) -> dict:
    address = body.address.strip()
    if not address:
        raise HTTPException(status_code=422, detail="Address must not be empty")

    lat, lng = await _geocode(address)

    await db.execute(
        sqlite_insert(AthleteProfile)
        .values(athlete_id=athlete_id, home_address=address, home_lat=lat, home_lng=lng)
        .on_conflict_do_update(
            index_elements=["athlete_id"],
            set_={"home_address": address, "home_lat": lat, "home_lng": lng},
        )
    )
    await db.commit()
    return {"home_address": address, "home_lat": lat, "home_lng": lng}


@router.post("/disconnect", responses={404: {"description": "No Strava connection found"}})
async def disconnect(athlete_id: CurrentAthlete, db: DB) -> dict:
    result = await db.execute(
        select(AthleteToken).where(AthleteToken.athlete_id == athlete_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="No Strava connection found")

    await db.execute(delete(AthleteToken).where(AthleteToken.athlete_id == athlete_id))
    await db.commit()
    return {"disconnected": True}


@router.delete("/account")
async def delete_account(athlete_id: CurrentAthlete, db: DB) -> dict:
    # Deletes all rows belonging to this athlete. SegmentEnrichment is intentionally
    # excluded — it has no athlete_id and its data (geometry, KOM times) is shared.
    for model in (
        AthleteToken,
        AthleteSyncState,
        AthleteProfile,
        AthleteRole,
        AthleteSegmentProfile,
        SegmentEffortDigest,
        SegmentEffortBackfillState,
    ):
        await db.execute(delete(model).where(model.athlete_id == athlete_id))
    await db.commit()
    return {"deleted": True}
