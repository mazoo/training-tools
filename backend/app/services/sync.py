import logging
from datetime import date, datetime, time as dt_time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.athlete import AthleteSyncState
from app.models.segment import AthleteSegmentProfile, SegmentEffortDigest, SegmentEnrichment
from app.strava.client import StravaClient, StravaRateLimitError
from app.tasks import TaskStatus
from app.utils import is_segment_indoor, xom_to_seconds

logger = logging.getLogger(__name__)

_BOOTSTRAP_DAYS = 180
_MAX_PAGES = 5
# KOM times rarely change — only re-fetch after this many days
_KOM_TIME_TTL_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def run_sync(
    db: AsyncSession,
    athlete_id: int,
    access_token: str,
    task: TaskStatus,
    full: bool = False,
) -> None:
    """
    Request order (mirrors attack-selector's low-API-call strategy):

      1. GET /segments/starred          — 1 call, know which segments are starred
      2. GET /athlete/activities        — 1–5 calls, get activity list
      3. GET /activities/{id}           — 1 call each, budget-limited
                                          segment metadata (name/grade/latlng) is FREE
                                          inside each effort object — no extra calls needed
      4. Recompute profiles             — local only, 0 calls
      5. GET /segments/{id}             — 1 call each, starred segments only,
                                          deferred to last so activities always run first,
                                          only fetches missing KOM times (xoms.kom)
    """
    try:
        client = StravaClient(access_token)

        # Step 1: starred list
        task.status = "running"
        logger.info("athlete=%d syncing starred segments", athlete_id)
        starred = await client.get_starred_segments()
        task.strava_calls_made += 1
        starred_ids = {s["id"] for s in starred}
        await _update_starred_flags(db, athlete_id, starred_ids)

        # Step 2: activity list
        after_ts = await _compute_after_ts(db, athlete_id, full)
        activities = await _fetch_activities(client, after_ts, task)
        task.activities_total = len(activities)
        logger.info("athlete=%d fetched %d activities to process", athlete_id, len(activities))

        # Step 3: activity details — segment metadata extracted for free here
        touched = await _fetch_activity_details(db, client, athlete_id, activities, task)

        # Step 4: local profile recomputation
        for segment_id in touched:
            await _recompute_profile(db, athlete_id, segment_id, starred_ids)

        await _upsert_sync_state(db, athlete_id, bootstrap_done=True)
        if task.status == "running":
            task.status = "done"

        # Step 5: KOM time enrichment — deferred, uses only leftover budget
        # Rate limiting here is non-fatal: candidate data is already usable.
        try:
            await _fetch_kom_times(db, client, starred, task)
        except StravaRateLimitError:
            logger.info("athlete=%d rate limited during KOM enrichment — will retry next sync", athlete_id)

    except StravaRateLimitError as exc:
        logger.warning("athlete=%d rate limited during core sync", athlete_id)
        task.status = "rate_limited"
        task.retry_after = _now() + timedelta(seconds=exc.retry_after_s)
    except Exception as exc:
        logger.exception("athlete=%d sync failed", athlete_id)
        task.status = "error"
        task.error = str(exc)


# ── orchestration helpers ──────────────────────────────────────────────────────

async def _compute_after_ts(db: AsyncSession, athlete_id: int, full: bool) -> int | None:
    sync_state = await _get_sync_state(db, athlete_id)
    if full or not sync_state or not sync_state.bootstrap_done:
        cutoff = _now() - timedelta(days=_BOOTSTRAP_DAYS)
        return int(cutoff.timestamp())
    if sync_state.last_activity_sync_at:
        return int(sync_state.last_activity_sync_at.timestamp())
    return None


async def _fetch_activities(
    client: StravaClient, after_ts: int | None, task: TaskStatus
) -> list[dict]:
    activities: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        page_data = await client.get_activities(after=after_ts, per_page=200, page=page)
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


async def _fetch_kom_times(
    db: AsyncSession, client: StravaClient, starred: list[dict], task: TaskStatus
) -> None:
    """Call GET /segments/{id} for starred segments that are missing a KOM time."""
    for seg in starred:
        segment_id = seg["id"]
        result = await db.execute(
            select(SegmentEnrichment).where(SegmentEnrichment.segment_id == segment_id)
        )
        existing = result.scalar_one_or_none()
        now = _now()

        # Skip if we already have a fresh KOM time
        if existing and existing.kom_time_s is not None:
            age_days = (now - existing.cached_at.replace(tzinfo=timezone.utc)).days
            if age_days < _KOM_TIME_TTL_DAYS:
                continue

        try:
            detail = await client.get_segment(segment_id)
            task.strava_calls_made += 1
        except StravaRateLimitError:
            raise
        except Exception:
            logger.warning("failed to fetch KOM time for segment=%d", segment_id)
            continue

        xoms = detail.get("xoms") or {}
        kom_time_s = xom_to_seconds(xoms.get("kom", ""))

        # Merge with whatever metadata we already have from activity processing
        latlng = detail.get("start_latlng") or []
        start_lat = latlng[0] if len(latlng) == 2 else None
        start_lng = latlng[1] if len(latlng) == 2 else None
        name = detail.get("name", "") or (existing.segment_name if existing else "")
        indoor = is_segment_indoor(detail)

        await db.execute(
            sqlite_insert(AthleteSegmentProfile)
            .values(
                athlete_id=0,
                segment_id=segment_id,
                segment_name=name,
                is_starred=True,
                is_indoor=indoor,
                times_ridden=0,
                top10_seen=False,
                podium_seen=False,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["athlete_id", "segment_id"],
                set_={"segment_name": name, "is_indoor": indoor},
            )
        )
        await db.execute(
            sqlite_insert(SegmentEnrichment)
            .values(
                segment_id=segment_id,
                segment_name=name,
                distance_m=detail.get("distance") or (existing.distance_m if existing else None),
                avg_grade_pct=detail.get("average_grade") or (existing.avg_grade_pct if existing else None),
                max_grade_pct=detail.get("maximum_grade") or (existing.max_grade_pct if existing else None),
                start_lat=start_lat or (existing.start_lat if existing else None),
                start_lng=start_lng or (existing.start_lng if existing else None),
                city=detail.get("city") or (existing.city if existing else None),
                country=detail.get("country") or (existing.country if existing else None),
                climb_category=detail.get("climb_category"),
                kom_time_s=kom_time_s,
                cached_at=now,
            )
            .on_conflict_do_update(
                index_elements=["segment_id"],
                set_={
                    "segment_name": name,
                    "avg_grade_pct": detail.get("average_grade") or (existing.avg_grade_pct if existing else None),
                    "max_grade_pct": detail.get("maximum_grade") or (existing.max_grade_pct if existing else None),
                    "start_lat": start_lat or (existing.start_lat if existing else None),
                    "start_lng": start_lng or (existing.start_lng if existing else None),
                    "city": detail.get("city") or (existing.city if existing else None),
                    "country": detail.get("country") or (existing.country if existing else None),
                    "climb_category": detail.get("climb_category"),
                    "kom_time_s": kom_time_s,
                    "cached_at": now,
                },
            )
        )
        await db.commit()


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_sync_state(db: AsyncSession, athlete_id: int) -> AthleteSyncState | None:
    result = await db.execute(
        select(AthleteSyncState).where(AthleteSyncState.athlete_id == athlete_id)
    )
    return result.scalar_one_or_none()


async def _upsert_sync_state(db: AsyncSession, athlete_id: int, bootstrap_done: bool) -> None:
    now = _now()
    stmt = (
        sqlite_insert(AthleteSyncState)
        .values(
            athlete_id=athlete_id,
            bootstrap_done=bootstrap_done,
            last_activity_sync_at=now,
            last_star_sync_at=now,
        )
        .on_conflict_do_update(
            index_elements=["athlete_id"],
            set_={
                "bootstrap_done": bootstrap_done,
                "last_activity_sync_at": now,
                "last_star_sync_at": now,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()


async def _update_starred_flags(
    db: AsyncSession, athlete_id: int, starred_ids: set[int]
) -> None:
    if not starred_ids:
        return
    for seg_id in starred_ids:
        stmt = (
            sqlite_insert(AthleteSegmentProfile)
            .values(
                athlete_id=athlete_id,
                segment_id=seg_id,
                segment_name="",
                is_starred=True,
                is_indoor=False,
                times_ridden=0,
                top10_seen=False,
                podium_seen=False,
                updated_at=_now(),
            )
            .on_conflict_do_update(
                index_elements=["athlete_id", "segment_id"],
                set_={"is_starred": True},
            )
        )
        await db.execute(stmt)
    await db.commit()


async def _process_activity(
    db: AsyncSession, athlete_id: int, activity: dict
) -> set[int]:
    """
    Extract effort digests AND segment metadata from one detailed activity.
    Segment name, grade, latlng etc. come free from effort["segment"] — no
    extra API calls needed.
    """
    touched: set[int] = set()
    efforts: list[dict] = activity.get("segment_efforts") or []
    activity_id = activity["id"]
    start_date = activity.get("start_date_local", activity.get("start_date", ""))[:10]
    try:
        effort_date = date.fromisoformat(start_date)
    except ValueError:
        effort_date = date.today()

    for effort in efforts:
        seg = effort.get("segment") or {}
        segment_id = seg.get("id")
        if not segment_id:
            continue

        # Effort digest
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

        # Segment metadata — free from the effort object, no extra API call
        latlng = seg.get("start_latlng") or []
        start_lat = latlng[0] if len(latlng) == 2 else None
        start_lng = latlng[1] if len(latlng) == 2 else None
        name = seg.get("name", "")
        indoor = is_segment_indoor(seg)

        await db.execute(
            sqlite_insert(SegmentEnrichment)
            .values(
                segment_id=segment_id,
                segment_name=name,
                distance_m=seg.get("distance"),
                avg_grade_pct=seg.get("average_grade"),
                max_grade_pct=seg.get("maximum_grade"),
                start_lat=start_lat,
                start_lng=start_lng,
                city=seg.get("city"),
                country=seg.get("country"),
                climb_category=seg.get("climb_category"),
                kom_time_s=None,  # populated later by _fetch_kom_times for starred segments
                cached_at=_now(),
            )
            .on_conflict_do_update(
                index_elements=["segment_id"],
                # Only overwrite geometry/meta if not already set to avoid
                # clobbering a previously fetched KOM time.
                set_={
                    "segment_name": name,
                    "distance_m": seg.get("distance"),
                    "avg_grade_pct": seg.get("average_grade"),
                    "max_grade_pct": seg.get("maximum_grade"),
                    "start_lat": start_lat,
                    "start_lng": start_lng,
                    "city": seg.get("city"),
                    "country": seg.get("country"),
                    "climb_category": seg.get("climb_category"),
                },
            )
        )

        # Keep profile name + indoor flag in sync
        if name:
            await db.execute(
                sqlite_insert(AthleteSegmentProfile)
                .values(
                    athlete_id=athlete_id,
                    segment_id=segment_id,
                    segment_name=name,
                    is_starred=False,
                    is_indoor=indoor,
                    times_ridden=0,
                    top10_seen=False,
                    podium_seen=False,
                    updated_at=_now(),
                )
                .on_conflict_do_update(
                    index_elements=["athlete_id", "segment_id"],
                    set_={"segment_name": name, "is_indoor": indoor},
                )
            )

        touched.add(segment_id)

    await db.commit()
    return touched


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

    times_ridden = len(efforts)
    best_time_s = min(e.elapsed_s for e in efforts)
    latest = efforts[-1]
    latest_time_s = latest.elapsed_s
    last_ridden_at = datetime.combine(latest.effort_date, dt_time.min, tzinfo=timezone.utc)
    last_seen_kom_rank = latest.kom_rank

    watts_values = [e.avg_watts for e in efforts if e.avg_watts is not None]
    best_avg_watts = max(watts_values) if watts_values else None
    latest_avg_watts = latest.avg_watts

    kom_ranks = [e.kom_rank for e in efforts if e.kom_rank is not None]
    top10_seen = any(r <= 10 for r in kom_ranks)
    podium_seen = any(r <= 3 for r in kom_ranks)
    best_seen_kom_rank = min(kom_ranks) if kom_ranks else None

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
    await db.execute(
        sqlite_insert(AthleteSegmentProfile)
        .values(
            athlete_id=athlete_id,
            segment_id=segment_id,
            segment_name=segment_name,
            is_starred=segment_id in starred_ids,
            is_indoor=is_indoor,
            times_ridden=times_ridden,
            best_time_s=best_time_s,
            latest_time_s=latest_time_s,
            best_avg_watts=best_avg_watts,
            latest_avg_watts=latest_avg_watts,
            top10_seen=top10_seen,
            podium_seen=podium_seen,
            best_seen_kom_rank=best_seen_kom_rank,
            last_seen_kom_rank=last_seen_kom_rank,
            last_ridden_at=last_ridden_at,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["athlete_id", "segment_id"],
            set_={
                "is_starred": segment_id in starred_ids,
                "times_ridden": times_ridden,
                "best_time_s": best_time_s,
                "latest_time_s": latest_time_s,
                "best_avg_watts": best_avg_watts,
                "latest_avg_watts": latest_avg_watts,
                "top10_seen": top10_seen,
                "podium_seen": podium_seen,
                "best_seen_kom_rank": best_seen_kom_rank,
                "last_seen_kom_rank": last_seen_kom_rank,
                "last_ridden_at": last_ridden_at,
                "updated_at": now,
            },
        )
    )
    await db.commit()
