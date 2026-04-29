import logging
from datetime import date, datetime, time as dt_time, timedelta, timezone
from dataclasses import dataclass

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.athlete import AthleteToken, AthleteSyncState
from app.models.segment import (
    AthleteSegmentProfile,
    SegmentEffortBackfillState,
    SegmentEffortDigest,
    SegmentEnrichment,
)
from app.strava.client import StravaClient, StravaRateLimitError
from app.strava.rate_limiter import rate_limiter
from app.tasks import TaskStatus, create_task
from app.utils import is_segment_indoor, xom_to_seconds

logger = logging.getLogger(__name__)

_FULL_REFRESH_DAYS = 30     # lookback window used when full=True is requested
_MAX_PAGES = 5
_STARRED_SEGMENTS_PER_PAGE = 200
_SEGMENT_EFFORTS_PER_PAGE = 200
ONBOARDING_STRAVA_CALL_BUDGET = 150
BACKFILL_CALLS_PER_15MIN_WINDOW = 10
# Stop a backfill run if fewer than this many daily calls remain — leaves
# headroom for ongoing syncs and starred-segment enrichment.
MIN_DAILY_BACKFILL_BUDGET = 150
_KOM_TIME_TTL = timedelta(days=7)
_BACKFILL_ROUND_ROBIN_INDEX = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── public entry points ────────────────────────────────────────────────────────

async def run_sync(
    db: AsyncSession,
    athlete_id: int,
    access_token: str,
    task: TaskStatus,
    full: bool = False,
) -> None:
    """
    User-triggered sync.

    Bootstrap (first connect): spends up to ONBOARDING_STRAVA_CALL_BUDGET
    calls on a gap-first first dataset. It fetches starred segments for free
    athlete PR metadata, then uses remaining calls for KOM-time enrichment.
    Effort history density is populated separately by the slow backfill.

    Incremental (subsequent calls): syncs starred segments + activities newer
    than last_activity_sync_at, so today's rides appear without waiting for
    the next backfill run.

    Request order (incremental):
      1. GET /segments/starred   — paginated (geometry + PR data free)
      2. GET /athlete/activities — 1–5 calls
      3. GET /activities/{id}    — 1 per new activity
      4. Profile recomputation   — local, 0 calls
    """
    try:
        client = StravaClient(access_token)

        task.status = "running"
        logger.info("athlete=%d syncing starred segments", athlete_id)
        starred = await _fetch_starred_segments(client, task)
        starred_ids = {s["id"] for s in starred}
        await _update_starred_flags(db, athlete_id, starred)

        sync_state = await _get_sync_state(db, athlete_id)
        if sync_state and sync_state.bootstrap_done:
            after_ts = _compute_after_ts(sync_state, full)
            activities = await _fetch_activities(client, after_ts=after_ts, task=task)
            task.activities_total = len(activities)
            logger.info("athlete=%d fetched %d activities to process", athlete_id, len(activities))
            touched = await _fetch_activity_details(db, client, athlete_id, activities, task)
            for segment_id in touched:
                await _recompute_profile(db, athlete_id, segment_id, starred_ids)
        else:
            remaining_budget = max(0, ONBOARDING_STRAVA_CALL_BUDGET - task.strava_calls_made)
            logger.info(
                "athlete=%d bootstrap: enriching KOM times with %d remaining onboarding calls",
                athlete_id,
                remaining_budget,
            )
            await _run_initial_kom_time_enrichment(
                db,
                client,
                athlete_id,
                task,
                call_budget=remaining_budget,
            )

        await _upsert_sync_state(db, athlete_id)
        if task.status == "running":
            task.status = "done"

    except StravaRateLimitError as exc:
        logger.warning("athlete=%d rate limited during core sync", athlete_id)
        task.status = "rate_limited"
        task.retry_after = _now() + timedelta(seconds=exc.retry_after_s)
    except Exception as exc:
        logger.exception("athlete=%d sync failed", athlete_id)
        task.status = "error"
        task.error = str(exc)


async def _backfill_one_segment(
    db: AsyncSession,
    client: StravaClient,
    athlete_id: int,
    segment_id: int,
    starred_ids: set[int],
    task: TaskStatus,
    call_budget: int | None = None,
) -> None:
    try:
        await _mark_segment_effort_backfill_attempt(db, athlete_id, segment_id)
        touched, complete = await _fetch_segment_efforts_paged(
            db,
            client,
            athlete_id,
            segment_id,
            task,
            call_budget=call_budget,
        )
        for touched_segment_id in touched:
            await _recompute_profile(db, athlete_id, touched_segment_id, starred_ids)
        if complete:
            await _mark_segment_effort_backfill_done(db, athlete_id, segment_id)
    except StravaRateLimitError:
        raise
    except Exception as exc:
        logger.warning("athlete=%d failed segment-effort backfill for segment=%d", athlete_id, segment_id)
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in {400, 403, 404}:
            await _mark_segment_effort_backfill_skipped(db, athlete_id, segment_id, str(exc))


async def _backfill_one_segment_kom_time(
    db: AsyncSession,
    client: StravaClient,
    athlete_id: int,
    segment_id: int,
    task: TaskStatus,
) -> None:
    try:
        detail = await client.get_segment(segment_id)
        task.strava_calls_made += 1
        xoms = detail.get("xoms") if isinstance(detail, dict) else None
        kom_time_s = xom_to_seconds(xoms.get("kom")) if isinstance(xoms, dict) else None
        await _mark_segment_kom_time_checked(db, segment_id, kom_time_s)
    except StravaRateLimitError:
        raise
    except Exception as exc:
        logger.warning("athlete=%d failed KOM-time backfill for segment=%d", athlete_id, segment_id)
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in {400, 403, 404}:
            await _mark_segment_kom_time_checked(db, segment_id, None)


async def run_backfill_chunk(
    db: AsyncSession,
    athlete_id: int,
    access_token: str,
    task: TaskStatus,
    call_budget: int | None = None,
) -> None:
    """
    Advance one athlete's enrichment/backfill work, optionally capped by a
    per-task Strava call budget. Daily background runs pass a small budget so
    a few parallel users can share the app-wide 15-minute window safely.

    Called by run_daily_backfill; also callable directly for testing.
    No-ops if initial sync hasn't run yet or no backfill work remains.
    """
    sync_state = await _get_sync_state(db, athlete_id)
    if not sync_state or not sync_state.bootstrap_done:
        task.status = "done"
        return

    try:
        client = StravaClient(access_token)
        task.status = "running"

        starred_ids = await _get_starred_ids(db, athlete_id)
        processed = 0

        kom_segment_ids = await _get_pending_kom_time_backfill_ids(db, athlete_id)
        if call_budget is not None:
            kom_segment_ids = kom_segment_ids[:call_budget]
        task.activities_total = len(kom_segment_ids)

        if kom_segment_ids:
            logger.info("athlete=%d KOM-time backfill: %d segments", athlete_id, len(kom_segment_ids))

        for segment_id in kom_segment_ids:
            if _call_budget_exhausted(task, call_budget):
                break
            await _backfill_one_segment_kom_time(db, client, athlete_id, segment_id, task)
            processed += 1
            task.activities_processed = processed

        segment_ids: list[int] = []
        if not _call_budget_exhausted(task, call_budget):
            remaining_call_budget = _remaining_call_budget(task, call_budget)
            segment_ids = await _get_pending_segment_effort_backfill_ids(db, athlete_id)
            if remaining_call_budget is not None:
                segment_ids = segment_ids[:remaining_call_budget]
            task.activities_total += len(segment_ids)

            if segment_ids:
                logger.info("athlete=%d segment-effort backfill: %d segments", athlete_id, len(segment_ids))

            for segment_id in segment_ids:
                if _call_budget_exhausted(task, call_budget):
                    break
                await _backfill_one_segment(
                    db,
                    client,
                    athlete_id,
                    segment_id,
                    starred_ids,
                    task,
                    call_budget=call_budget,
                )
                processed += 1
                task.activities_processed = processed

            if not await _get_pending_segment_effort_backfill_ids(db, athlete_id):
                await _mark_backfill_complete(db, athlete_id)

        task.status = "done"

    except StravaRateLimitError as exc:
        logger.warning("athlete=%d rate limited during backfill", athlete_id)
        task.status = "rate_limited"
        task.retry_after = _now() + timedelta(seconds=exc.retry_after_s)
    except Exception as exc:
        logger.exception("athlete=%d backfill chunk failed", athlete_id)
        task.status = "error"
        task.error = str(exc)


async def run_daily_backfill(db: AsyncSession) -> dict[int, str]:
    """
    Process one backfill chunk per athlete that still needs historical data.
    Designed to be called once per day by the cron endpoint.

    Multi-user budget safety: at most BACKFILL_CALLS_PER_15MIN_WINDOW calls are
    spent per invocation across all athletes. Athletes are rotated round-robin
    so one dense account cannot starve another account indefinitely.
    """
    from app.services.auth import get_valid_access_token

    global _BACKFILL_ROUND_ROBIN_INDEX

    result = await db.execute(select(AthleteToken))
    all_tokens = sorted(result.scalars().all(), key=lambda row: row.athlete_id)
    if not all_tokens:
        return {}

    start_index = _BACKFILL_ROUND_ROBIN_INDEX % len(all_tokens)
    token_rows = all_tokens[start_index:] + all_tokens[:start_index]

    outcomes: dict[int, str] = {}
    calls_spent = 0

    for offset, token_row in enumerate(token_rows):
        athlete_id = token_row.athlete_id
        if calls_spent >= BACKFILL_CALLS_PER_15MIN_WINDOW:
            outcomes[athlete_id] = "budget_deferred"
            continue

        sync_state = await _get_sync_state(db, athlete_id)
        if not sync_state or not sync_state.bootstrap_done:
            outcomes[athlete_id] = "skipped"
            continue

        if sync_state.backfill_complete and not await _get_pending_kom_time_backfill_ids(db, athlete_id):
            outcomes[athlete_id] = "skipped"
            continue

        if rate_limiter.remaining_daily < MIN_DAILY_BACKFILL_BUDGET:
            logger.warning("daily budget low (%d remaining) — stopping backfill", rate_limiter.remaining_daily)
            for row in all_tokens:
                if row.athlete_id not in outcomes:
                    outcomes[row.athlete_id] = "budget_exhausted"
            break

        task = create_task()
        try:
            access_token = await get_valid_access_token(athlete_id, db)
            remaining_window_budget = BACKFILL_CALLS_PER_15MIN_WINDOW - calls_spent
            await run_backfill_chunk(
                db,
                athlete_id,
                access_token,
                task,
                call_budget=remaining_window_budget,
            )
            calls_spent += task.strava_calls_made
            outcomes[athlete_id] = task.status
            if task.strava_calls_made > 0:
                _BACKFILL_ROUND_ROBIN_INDEX = (start_index + offset + 1) % len(all_tokens)
        except Exception as exc:
            logger.exception("athlete=%d daily backfill failed", athlete_id)
            outcomes[athlete_id] = f"error: {exc}"

    return outcomes


# ── orchestration helpers ──────────────────────────────────────────────────────

def _remaining_call_budget(task: TaskStatus, call_budget: int | None) -> int | None:
    if call_budget is None:
        return None
    return max(0, call_budget - task.strava_calls_made)


def _call_budget_exhausted(task: TaskStatus, call_budget: int | None) -> bool:
    remaining = _remaining_call_budget(task, call_budget)
    return remaining is not None and remaining <= 0


def _compute_after_ts(sync_state: AthleteSyncState, full: bool) -> int:
    fallback = int((_now() - timedelta(days=_FULL_REFRESH_DAYS)).timestamp())
    if full or not sync_state.last_activity_sync_at:
        return fallback
    return int(sync_state.last_activity_sync_at.timestamp())


async def _fetch_starred_segments(
    client: StravaClient,
    task: TaskStatus,
) -> list[dict]:
    starred: list[dict] = []
    page = 1
    while True:
        page_data = await client.get_starred_segments_page(
            page=page,
            per_page=_STARRED_SEGMENTS_PER_PAGE,
        )
        task.strava_calls_made += 1
        if not page_data:
            break
        starred.extend(page_data)
        if len(page_data) < _STARRED_SEGMENTS_PER_PAGE:
            break
        page += 1
    return starred


async def _run_initial_kom_time_enrichment(
    db: AsyncSession,
    client: StravaClient,
    athlete_id: int,
    task: TaskStatus,
    call_budget: int,
) -> None:
    if call_budget <= 0:
        return

    segment_ids = (await _get_pending_kom_time_backfill_ids(db, athlete_id))[:call_budget]
    if not segment_ids:
        return

    task.activities_total = len(segment_ids)
    for segment_id in segment_ids:
        if _call_budget_exhausted(task, ONBOARDING_STRAVA_CALL_BUDGET):
            break
        await _backfill_one_segment_kom_time(db, client, athlete_id, segment_id, task)
        task.activities_processed += 1


async def _fetch_activities(
    client: StravaClient,
    task: TaskStatus,
    after_ts: int | None = None,
    before_ts: int | None = None,
) -> list[dict]:
    activities: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        page_data = await client.get_activities(
            after=after_ts, before=before_ts, per_page=200, page=page
        )
        task.strava_calls_made += 1
        activities.extend(page_data)
        if len(page_data) < 200:
            break
    return activities


async def _fetch_activity_details(
    db: AsyncSession,
    client: StravaClient,
    athlete_id: int,
    activities: list[dict],
    task: TaskStatus,
) -> set[int]:
    touched: set[int] = set()
    for i, activity in enumerate(activities):
        try:
            detail = await client.get_activity(activity["id"])
            task.strava_calls_made += 1
            segs = await _process_activity(db, athlete_id, detail)
            touched.update(segs)
        except StravaRateLimitError:
            raise
        except Exception:
            logger.warning("athlete=%d failed to process activity=%d", athlete_id, activity["id"])
        task.activities_processed = i + 1
    return touched


async def _fetch_segment_efforts_paged(
    db: AsyncSession,
    client: StravaClient,
    athlete_id: int,
    segment_id: int,
    task: TaskStatus,
    call_budget: int | None = None,
) -> tuple[set[int], bool]:
    touched: set[int] = set()
    page = 1
    while True:
        if _call_budget_exhausted(task, call_budget):
            return touched, False
        efforts = await client.get_segment_efforts(segment_id=segment_id, per_page=_SEGMENT_EFFORTS_PER_PAGE, page=page)
        task.strava_calls_made += 1
        touched.update(await _process_segment_efforts(db, athlete_id, efforts))
        if len(efforts) < _SEGMENT_EFFORTS_PER_PAGE:
            return touched, True
        page += 1


def _latlng(raw: object) -> tuple[float | None, float | None]:
    coords = raw if isinstance(raw, list) else []
    return (coords[0], coords[1]) if len(coords) == 2 else (None, None)


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_sync_state(db: AsyncSession, athlete_id: int) -> AthleteSyncState | None:
    result = await db.execute(
        select(AthleteSyncState).where(AthleteSyncState.athlete_id == athlete_id)
    )
    return result.scalar_one_or_none()


async def _get_starred_ids(db: AsyncSession, athlete_id: int) -> set[int]:
    result = await db.execute(
        select(AthleteSegmentProfile.segment_id).where(
            AthleteSegmentProfile.athlete_id == athlete_id,
            AthleteSegmentProfile.is_starred.is_(True),
        )
    )
    return {row[0] for row in result.all()}


_ACTIVITY_TYPE_PRIORITY = {"Ride": 0, "Run": 1}


def _ts(value: datetime | None) -> float:
    return value.timestamp() if value else 0.0


def _known_effort_time_s(profile: AthleteSegmentProfile) -> int | None:
    return profile.best_time_s or profile.pr_time_s


def _duration_bucket(seconds: int | None) -> int:
    if seconds is None:
        return 4
    if 20 <= seconds <= 1800:
        return 0
    if 1800 < seconds <= 3600:
        return 1
    if seconds < 20:
        return 2
    return 3


def _grade_bucket(grade: float | None) -> int:
    if grade is None:
        return 2
    if -5.0 <= grade <= 12.0:
        return 0
    if -10.0 <= grade <= 18.0:
        return 1
    return 2


def _best_chance_sort_key(
    profile: AthleteSegmentProfile,
    enrichment: SegmentEnrichment | None,
) -> tuple:
    known_time = _known_effort_time_s(profile)
    activity_type = enrichment.activity_type if enrichment else None
    latest_interest = max(
        _ts(profile.pr_date),
        _ts(profile.starred_date),
        _ts(profile.last_ridden_at),
    )
    return (
        0 if profile.pr_time_s is not None else 1,
        1 if profile.is_indoor else 0,
        _ACTIVITY_TYPE_PRIORITY.get(activity_type or "", 2),
        _duration_bucket(known_time),
        _grade_bucket(enrichment.avg_grade_pct if enrichment else None),
        known_time if known_time is not None else 999_999_999,
        -latest_interest,
        profile.segment_id,
    )


def _has_kom_time(profile: AthleteSegmentProfile, enrichment: SegmentEnrichment | None) -> bool:
    return bool(
        (enrichment and enrichment.kom_time_s is not None)
        or (profile.is_kom and profile.pr_time_s is not None)
    )


async def _get_pending_segment_effort_backfill_ids(db: AsyncSession, athlete_id: int) -> list[int]:
    sync_state = await _get_sync_state(db, athlete_id)
    profiles_result = await db.execute(
        select(AthleteSegmentProfile, SegmentEnrichment)
        .outerjoin(SegmentEnrichment, SegmentEnrichment.segment_id == AthleteSegmentProfile.segment_id)
        .where(
            AthleteSegmentProfile.athlete_id == athlete_id,
            AthleteSegmentProfile.is_starred.is_(True),
        )
    )
    rows = profiles_result.all()

    state_result = await db.execute(
        select(SegmentEffortBackfillState).where(
            SegmentEffortBackfillState.athlete_id == athlete_id,
        )
    )
    states = {row.segment_id: row for row in state_result.scalars().all()}

    effort_counts_result = await db.execute(
        select(SegmentEffortDigest.segment_id, func.count(SegmentEffortDigest.effort_id))
        .where(SegmentEffortDigest.athlete_id == athlete_id)
        .group_by(SegmentEffortDigest.segment_id)
    )
    effort_counts = {segment_id: count for segment_id, count in effort_counts_result.all()}

    def should_backfill(profile: AthleteSegmentProfile) -> bool:
        state = states.get(profile.segment_id)
        if state is None or state.status == "pending":
            return True
        if state.status == "skipped":
            return False
        if state.status != "done":
            return True
        if effort_counts.get(profile.segment_id, 0) > 0:
            return False

        # Older backfills may have marked PR-seeded segments done even when no
        # segment-effort rows were imported. Retry once after a newer starred
        # sync confirms the segment still has athlete_pr_effort metadata.
        return bool(
            profile.pr_time_s is not None
            and sync_state
            and sync_state.last_star_sync_at
            and (state.completed_at is None or _ts(state.completed_at) < _ts(sync_state.last_star_sync_at))
        )

    pending = [(profile, enrichment) for profile, enrichment in rows if should_backfill(profile)]

    pending.sort(
        key=lambda row: (
            0 if _has_kom_time(row[0], row[1]) else 1,
            *_best_chance_sort_key(row[0], row[1]),
        )
    )
    return [profile.segment_id for profile, _ in pending]


async def _get_pending_kom_time_backfill_ids(db: AsyncSession, athlete_id: int) -> list[int]:
    stale_before = _now() - _KOM_TIME_TTL
    result = await db.execute(
        select(AthleteSegmentProfile, SegmentEnrichment)
        .outerjoin(SegmentEnrichment, SegmentEnrichment.segment_id == AthleteSegmentProfile.segment_id)
        .where(
            AthleteSegmentProfile.athlete_id == athlete_id,
            AthleteSegmentProfile.is_starred.is_(True),
            AthleteSegmentProfile.is_kom.is_(False),
            or_(
                AthleteSegmentProfile.pr_time_s.is_not(None),
                AthleteSegmentProfile.best_time_s.is_not(None),
                AthleteSegmentProfile.times_ridden > 0,
            ),
            or_(
                SegmentEnrichment.kom_time_checked_at.is_(None),
                SegmentEnrichment.kom_time_checked_at < stale_before,
            ),
        )
    )
    pending = result.all()
    pending.sort(
        key=lambda row: (
            0 if row[0].podium_seen else 1,
            0 if row[0].top10_seen else 1,
            row[0].best_seen_kom_rank or 99,
            *_best_chance_sort_key(row[0], row[1]),
        )
    )
    return [profile.segment_id for profile, _ in pending]


async def _mark_segment_effort_backfill_attempt(
    db: AsyncSession, athlete_id: int, segment_id: int
) -> None:
    now = _now()
    stmt = (
        sqlite_insert(SegmentEffortBackfillState)
        .values(
            athlete_id=athlete_id,
            segment_id=segment_id,
            status="pending",
            completed_at=None,
            last_attempt_at=now,
            last_error=None,
        )
        .on_conflict_do_update(
            index_elements=["athlete_id", "segment_id"],
            set_={"status": "pending", "last_attempt_at": now, "last_error": None},
        )
    )
    await db.execute(stmt)
    await db.commit()


async def _mark_segment_effort_backfill_done(
    db: AsyncSession, athlete_id: int, segment_id: int
) -> None:
    now = _now()
    stmt = (
        sqlite_insert(SegmentEffortBackfillState)
        .values(
            athlete_id=athlete_id,
            segment_id=segment_id,
            status="done",
            completed_at=now,
            last_attempt_at=now,
            last_error=None,
        )
        .on_conflict_do_update(
            index_elements=["athlete_id", "segment_id"],
            set_={
                "status": "done",
                "completed_at": now,
                "last_attempt_at": now,
                "last_error": None,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()


async def _mark_segment_effort_backfill_skipped(
    db: AsyncSession, athlete_id: int, segment_id: int, error: str
) -> None:
    now = _now()
    stmt = (
        sqlite_insert(SegmentEffortBackfillState)
        .values(
            athlete_id=athlete_id,
            segment_id=segment_id,
            status="skipped",
            completed_at=now,
            last_attempt_at=now,
            last_error=error[:500],
        )
        .on_conflict_do_update(
            index_elements=["athlete_id", "segment_id"],
            set_={
                "status": "skipped",
                "completed_at": now,
                "last_attempt_at": now,
                "last_error": error[:500],
            },
        )
    )
    await db.execute(stmt)
    await db.commit()


async def _mark_segment_kom_time_checked(
    db: AsyncSession, segment_id: int, kom_time_s: int | None
) -> None:
    now = _now()
    values = {
        "segment_id": segment_id,
        "kom_time_s": kom_time_s,
        "kom_time_checked_at": now,
        "cached_at": now,
    }
    stmt = (
        sqlite_insert(SegmentEnrichment)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["segment_id"],
            set_={
                "kom_time_s": kom_time_s,
                "kom_time_checked_at": now,
                "cached_at": now,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()


async def _mark_backfill_complete(db: AsyncSession, athlete_id: int) -> None:
    await db.execute(
        update(AthleteSyncState)
        .where(AthleteSyncState.athlete_id == athlete_id)
        .values(backfill_complete=True)
    )
    await db.commit()


async def _upsert_sync_state(db: AsyncSession, athlete_id: int) -> None:
    now = _now()
    update_set = {
        "bootstrap_done": True,
        "last_activity_sync_at": now,
        "last_star_sync_at": now,
        # Reset so the next backfill run picks up any newly starred segments.
        "backfill_complete": False,
    }
    stmt = (
        sqlite_insert(AthleteSyncState)
        .values(athlete_id=athlete_id, **update_set)
        .on_conflict_do_update(index_elements=["athlete_id"], set_=update_set)
    )
    await db.execute(stmt)
    await db.commit()


async def _update_starred_flags(
    db: AsyncSession, athlete_id: int, starred: list[dict]
) -> None:
    """
    Upsert starred segment data from GET /segments/starred.

    Segment-level fields (geometry, grade, elevation, activity_type…) go into
    segment_enrichment. Athlete-specific fields (PR, is_kom, starred_date) go
    into athlete_segment_profile. cached_at and kom_time_s in segment_enrichment
    are intentionally NOT updated here — those are managed by the KOM-time backfill.
    """
    now = _now()
    for seg in starred:
        seg_id = seg["id"]
        name = seg.get("name", "")
        indoor = is_segment_indoor(seg)

        pr = seg.get("athlete_pr_effort") or {}
        pr_time_s = pr.get("elapsed_time")
        pr_activity_id = pr.get("activity_id")
        pr_date = _parse_iso(pr.get("start_date"))
        is_kom = bool(pr.get("is_kom", False))
        starred_date = _parse_iso(seg.get("starred_date"))

        athlete_update = {
            "is_starred": True,
            "is_indoor": indoor,
            "is_kom": is_kom,
            "pr_time_s": pr_time_s,
            "pr_activity_id": pr_activity_id,
            "pr_date": pr_date,
            "starred_date": starred_date,
        }
        await db.execute(
            sqlite_insert(AthleteSegmentProfile)
            .values(
                athlete_id=athlete_id, segment_id=seg_id, segment_name=name,
                is_starred=True, is_indoor=indoor, times_ridden=0,
                top10_seen=False, podium_seen=False, updated_at=now,
                is_kom=is_kom, pr_time_s=pr_time_s, pr_activity_id=pr_activity_id,
                pr_date=pr_date, starred_date=starred_date,
            )
            .on_conflict_do_update(
                index_elements=["athlete_id", "segment_id"],
                set_=athlete_update,
            )
        )

        start_lat, start_lng = _latlng(seg.get("start_latlng"))
        end_lat, end_lng = _latlng(seg.get("end_latlng"))
        # Only update geometry/metadata fields — preserve cached_at and kom_time_s.
        enrichment_update = {
            "segment_name": name,
            "distance_m": seg.get("distance"),
            "avg_grade_pct": seg.get("average_grade"),
            "max_grade_pct": seg.get("maximum_grade"),
            "start_lat": start_lat,
            "start_lng": start_lng,
            "end_lat": end_lat,
            "end_lng": end_lng,
            "city": seg.get("city"),
            "country": seg.get("country"),
            "state": seg.get("state"),
            "climb_category": seg.get("climb_category"),
            "elevation_high": seg.get("elevation_high"),
            "elevation_low": seg.get("elevation_low"),
            "activity_type": seg.get("activity_type"),
            "hazardous": seg.get("hazardous"),
        }
        enrichment_values = {
            "segment_id": seg_id,
            "kom_time_s": pr_time_s if is_kom and pr_time_s else None,
            "kom_time_checked_at": now if is_kom and pr_time_s else None,
            "cached_at": now,
            **enrichment_update,
        }
        enrichment_conflict_update = dict(enrichment_update)
        if is_kom and pr_time_s:
            # If the athlete currently owns the KOM/QOM, their PR is the
            # current XOM time, so gap-to-KOM is known without a detail call.
            enrichment_conflict_update.update(
                {
                    "kom_time_s": pr_time_s,
                    "kom_time_checked_at": now,
                }
            )
        await db.execute(
            sqlite_insert(SegmentEnrichment)
            .values(**enrichment_values)
            .on_conflict_do_update(index_elements=["segment_id"], set_=enrichment_conflict_update)
        )

    if starred:
        await db.commit()


async def _process_activity(
    db: AsyncSession, athlete_id: int, activity: dict
) -> set[int]:
    """
    Extract effort digests and segment metadata from one detailed activity.
    Segment name, grade, latlng etc. come free from effort["segment"].
    """
    efforts: list[dict] = activity.get("segment_efforts") or []
    activity_id = activity["id"]
    effort_date = _parse_effort_date(activity)
    touched: set[int] = set()

    for effort in efforts:
        seg = effort.get("segment") or {}
        segment_id = seg.get("id")
        if not segment_id:
            continue
        await _upsert_effort_digest(db, effort, athlete_id, segment_id, activity_id, effort_date)
        await _upsert_enrichment_from_effort(db, seg, segment_id)
        await _upsert_profile_name(db, athlete_id, seg, segment_id)
        touched.add(segment_id)

    await db.commit()
    return touched


async def _process_segment_efforts(
    db: AsyncSession, athlete_id: int, efforts: list[dict]
) -> set[int]:
    """
    Store DetailedSegmentEffort rows returned by GET /segment_efforts.
    """
    touched: set[int] = set()

    for effort in efforts:
        seg = effort.get("segment") or {}
        segment_id = seg.get("id")
        activity = effort.get("activity") or {}
        activity_id = activity.get("id")
        if not segment_id or not activity_id:
            continue

        effort_date = _parse_segment_effort_date(effort)
        await _upsert_effort_digest(db, effort, athlete_id, segment_id, activity_id, effort_date)
        await _upsert_enrichment_from_effort(db, seg, segment_id)
        await _upsert_profile_name(db, athlete_id, seg, segment_id)
        touched.add(segment_id)

    await db.commit()
    return touched


def _parse_date(raw: str | None) -> date:
    raw_date = (raw or "")[:10]
    try:
        return date.fromisoformat(raw_date)
    except ValueError:
        return date.today()


def _parse_effort_date(activity: dict) -> date:
    return _parse_date(activity.get("start_date_local") or activity.get("start_date"))


def _parse_segment_effort_date(effort: dict) -> date:
    return _parse_date(effort.get("start_date_local") or effort.get("start_date"))


async def _upsert_effort_digest(
    db: AsyncSession,
    effort: dict,
    athlete_id: int,
    segment_id: int,
    activity_id: int,
    effort_date: date,
) -> None:
    await db.execute(
        sqlite_insert(SegmentEffortDigest)
        .values(
            effort_id=effort["id"],
            athlete_id=athlete_id,
            segment_id=segment_id,
            activity_id=activity_id,
            effort_date=effort_date,
            elapsed_s=effort.get("elapsed_time", 0),
            moving_s=effort.get("moving_time"),
            avg_watts=effort.get("average_watts"),
            kom_rank=effort.get("kom_rank"),
            pr_rank=effort.get("pr_rank"),
        )
        .on_conflict_do_update(
            index_elements=["effort_id"],
            set_={
                "avg_watts": effort.get("average_watts"),
                "kom_rank": effort.get("kom_rank"),
                "pr_rank": effort.get("pr_rank"),
            },
        )
    )


async def _upsert_enrichment_from_effort(
    db: AsyncSession, seg: dict, segment_id: int
) -> None:
    latlng = seg.get("start_latlng") or []
    start_lat = latlng[0] if len(latlng) == 2 else None
    start_lng = latlng[1] if len(latlng) == 2 else None
    meta = {
        "segment_name": seg.get("name", ""),
        "distance_m": seg.get("distance"),
        "avg_grade_pct": seg.get("average_grade"),
        "max_grade_pct": seg.get("maximum_grade"),
        "start_lat": start_lat,
        "start_lng": start_lng,
        "city": seg.get("city"),
        "country": seg.get("country"),
        "climb_category": seg.get("climb_category"),
    }
    await db.execute(
        sqlite_insert(SegmentEnrichment)
        .values(segment_id=segment_id, kom_time_s=None, cached_at=_now(), **meta)
        .on_conflict_do_update(index_elements=["segment_id"], set_=meta)
    )


async def _upsert_profile_name(
    db: AsyncSession, athlete_id: int, seg: dict, segment_id: int
) -> None:
    name = seg.get("name", "")
    if not name:
        return
    indoor = is_segment_indoor(seg)
    await db.execute(
        sqlite_insert(AthleteSegmentProfile)
        .values(
            athlete_id=athlete_id, segment_id=segment_id, segment_name=name,
            is_starred=False, is_indoor=indoor, times_ridden=0,
            top10_seen=False, podium_seen=False, updated_at=_now(),
        )
        .on_conflict_do_update(
            index_elements=["athlete_id", "segment_id"],
            set_={"segment_name": name, "is_indoor": indoor},
        )
    )


@dataclass
class _ProfileStats:
    times_ridden: int
    best_time_s: int
    latest_time_s: int
    best_avg_watts: float | None
    latest_avg_watts: float | None
    top10_seen: bool
    podium_seen: bool
    best_seen_kom_rank: int | None
    last_seen_kom_rank: int | None
    last_ridden_at: datetime


def _compute_profile_stats(efforts: list) -> _ProfileStats:
    times_ridden = len(efforts)
    best_time_s = min(e.elapsed_s for e in efforts)
    latest = efforts[-1]
    watts = [e.avg_watts for e in efforts if e.avg_watts is not None]
    ranks = [e.kom_rank for e in efforts if e.kom_rank is not None]
    return _ProfileStats(
        times_ridden=times_ridden,
        best_time_s=best_time_s,
        latest_time_s=latest.elapsed_s,
        best_avg_watts=max(watts) if watts else None,
        latest_avg_watts=latest.avg_watts,
        top10_seen=any(r <= 10 for r in ranks),
        podium_seen=any(r <= 3 for r in ranks),
        best_seen_kom_rank=min(ranks) if ranks else None,
        last_seen_kom_rank=latest.kom_rank,
        last_ridden_at=datetime.combine(latest.effort_date, dt_time.min, tzinfo=timezone.utc),
    )


async def _recompute_profile(
    db: AsyncSession, athlete_id: int, segment_id: int, starred_ids: set[int]
) -> None:
    result = await db.execute(
        select(SegmentEffortDigest)
        .where(
            SegmentEffortDigest.athlete_id == athlete_id,
            SegmentEffortDigest.segment_id == segment_id,
        )
        .order_by(SegmentEffortDigest.effort_date)
    )
    efforts = result.scalars().all()
    if not efforts:
        return

    stats = _compute_profile_stats(efforts)

    existing = await db.execute(
        select(AthleteSegmentProfile).where(
            AthleteSegmentProfile.athlete_id == athlete_id,
            AthleteSegmentProfile.segment_id == segment_id,
        )
    )
    existing_row = existing.scalar_one_or_none()
    segment_name = existing_row.segment_name if existing_row and existing_row.segment_name else ""
    is_indoor = existing_row.is_indoor if existing_row else False

    now = _now()
    profile = {
        "segment_name": segment_name,
        "is_starred": segment_id in starred_ids,
        "is_indoor": is_indoor,
        "times_ridden": stats.times_ridden,
        "best_time_s": stats.best_time_s,
        "latest_time_s": stats.latest_time_s,
        "best_avg_watts": stats.best_avg_watts,
        "latest_avg_watts": stats.latest_avg_watts,
        "top10_seen": stats.top10_seen,
        "podium_seen": stats.podium_seen,
        "best_seen_kom_rank": stats.best_seen_kom_rank,
        "last_seen_kom_rank": stats.last_seen_kom_rank,
        "last_ridden_at": stats.last_ridden_at,
        "updated_at": now,
    }
    await db.execute(
        sqlite_insert(AthleteSegmentProfile)
        .values(athlete_id=athlete_id, segment_id=segment_id, **profile)
        .on_conflict_do_update(index_elements=["athlete_id", "segment_id"], set_=profile)
    )
    await db.commit()


async def reset_kom_time_checked(db: AsyncSession) -> int:
    """
    Clear kom_time_checked_at for all enriched segments where kom_time_s is still
    null. This re-queues them for the next backfill run so the KOM-time fetch is
    retried (useful when Strava previously returned a segment without xoms but now
    has the data available).

    Returns the number of rows reset.
    """
    result = await db.execute(
        update(SegmentEnrichment)
        .where(
            SegmentEnrichment.kom_time_s.is_(None),
            SegmentEnrichment.kom_time_checked_at.is_not(None),
        )
        .values(kom_time_checked_at=None)
    )
    await db.commit()
    return result.rowcount
