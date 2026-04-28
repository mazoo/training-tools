from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.kom_qom import (
    CandidateFilters,
    CandidatesResponse,
    RefreshResponse,
    RefreshStatusResponse,
)
from app.services.auth import get_current_athlete_id, get_valid_access_token
from app.services.kom_qom import get_candidates
from app.services.sync import run_sync
from app.strava.client import StravaRateLimitError
from app.strava.rate_limiter import rate_limiter
from app.tasks import create_task, get_task

router = APIRouter(prefix="/api/kom-qom", tags=["kom-qom"])


@router.get("/candidates", response_model=CandidatesResponse)
async def candidates(
    effort_time_min: int | None = None,
    effort_time_max: int | None = None,
    gradient_min: float | None = None,
    gradient_max: float | None = None,
    surface: str = "all",
    podium_only: bool = False,
    athlete_id: int = Depends(get_current_athlete_id),
    db: AsyncSession = Depends(get_db),
) -> CandidatesResponse:
    filters = CandidateFilters(
        effort_time_min=effort_time_min,
        effort_time_max=effort_time_max,
        gradient_min=gradient_min,
        gradient_max=gradient_max,
        surface=surface,
        podium_only=podium_only,
    )
    try:
        return await get_candidates(db, athlete_id, filters)
    except StravaRateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail={"strava_error": "Rate limit reached", "retry_after_s": exc.retry_after_s},
        )


@router.post("/refresh", response_model=RefreshResponse, status_code=202)
async def refresh(
    background_tasks: BackgroundTasks,
    full: bool = False,
    athlete_id: int = Depends(get_current_athlete_id),
    db: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    access_token = await get_valid_access_token(athlete_id, db)
    task = create_task()

    async def _run() -> None:
        from app.database import AsyncSessionLocal

        async with AsyncSessionLocal() as bg_db:
            await run_sync(bg_db, athlete_id, access_token, task, full=full)

    background_tasks.add_task(_run)

    return RefreshResponse(task_id=task.task_id, message="Refresh started")


@router.get("/refresh/{task_id}", response_model=RefreshStatusResponse)
async def refresh_status(task_id: str) -> RefreshStatusResponse:
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return RefreshStatusResponse(
        status=task.status,
        activities_processed=task.activities_processed,
        activities_total=task.activities_total,
        strava_calls_made=task.strava_calls_made,
        strava_budget_remaining_15min=rate_limiter.remaining_15min,
        error=task.error,
        retry_after=task.retry_after,
    )
