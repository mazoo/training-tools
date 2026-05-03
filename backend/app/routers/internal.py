from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services.sync import reset_kom_time_checked, run_daily_backfill

router = APIRouter(prefix="/api/internal", tags=["internal"])


def _require_backfill_secret(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {settings.backfill_secret}"
    if not authorization or authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post("/daily-backfill")
async def daily_backfill(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_backfill_secret),
) -> dict:
    """
    Advance each athlete's starred-segment effort and XOM-time backfills.
    Designed to be called once per day by a system cron job:

        curl -X POST http://localhost:8000/api/internal/daily-backfill \
             -H "Authorization: Bearer <BACKFILL_SECRET>"

    Returns a per-athlete outcome map: "done" | "skipped" | "rate_limited" | "budget_exhausted" | "error: ..."
    """
    outcomes = await run_daily_backfill(db)
    return {"outcomes": outcomes}


@router.post("/reset-kom-time-checked")
async def reset_kom_time_checked_endpoint(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_backfill_secret),
) -> dict:
    """
    Re-queue all segments whose KOM/QOM time was fetched but came back null.
    The next backfill run will re-call GET /segments/{id} for each of them.

        curl -X POST http://localhost:8000/api/internal/reset-kom-time-checked \
             -H "Authorization: Bearer <BACKFILL_SECRET>"
    """
    rows_reset = await reset_kom_time_checked(db)
    return {"rows_reset": rows_reset}
