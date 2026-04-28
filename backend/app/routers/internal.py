from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services.sync import run_daily_backfill

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
    Advance each athlete's starred-segment effort backfill.
    Designed to be called once per day by a system cron job:

        curl -X POST http://localhost:8000/api/internal/daily-backfill \
             -H "Authorization: Bearer <BACKFILL_SECRET>"

    Returns a per-athlete outcome map: "done" | "skipped" | "rate_limited" | "budget_exhausted" | "error: ..."
    """
    outcomes = await run_daily_backfill(db)
    return {"outcomes": outcomes}
