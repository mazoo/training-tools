from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.athlete import AthleteProfile
from app.models.segment import AthleteSegmentProfile, SegmentEnrichment
from app.schemas.kom_qom import CandidateFilters, CandidatesResponse, SegmentCandidate
from app.utils import haversine_km, seconds_to_display


def _apply_filters(stmt, filters: CandidateFilters):
    if filters.podium_only:
        stmt = stmt.where(AthleteSegmentProfile.podium_seen == True)
    if filters.effort_time_min is not None:
        stmt = stmt.where(AthleteSegmentProfile.best_time_s >= filters.effort_time_min)
    if filters.effort_time_max is not None:
        stmt = stmt.where(AthleteSegmentProfile.best_time_s <= filters.effort_time_max)
    if filters.gradient_min is not None:
        stmt = stmt.where(SegmentEnrichment.avg_grade_pct >= filters.gradient_min)
    if filters.gradient_max is not None:
        stmt = stmt.where(SegmentEnrichment.avg_grade_pct <= filters.gradient_max)
    if filters.surface == "outdoor":
        stmt = stmt.where(AthleteSegmentProfile.is_indoor == False)
    elif filters.surface == "indoor":
        stmt = stmt.where(AthleteSegmentProfile.is_indoor == True)
    return stmt


def _distance_from_home(
    enrichment: SegmentEnrichment | None,
    home_lat: float | None,
    home_lng: float | None,
) -> float | None:
    if (
        home_lat is None
        or home_lng is None
        or enrichment is None
        or enrichment.start_lat is None
        or enrichment.start_lng is None
    ):
        return None
    return round(haversine_km(home_lat, home_lng, enrichment.start_lat, enrichment.start_lng), 1)


def _gap_to_kom(
    profile: AthleteSegmentProfile, enrichment: SegmentEnrichment | None
) -> tuple[int | None, float | None]:
    kom_time_s = enrichment.kom_time_s if enrichment else None
    if not kom_time_s or not profile.best_time_s:
        return None, None
    gap_s = profile.best_time_s - kom_time_s
    return gap_s, round(gap_s / kom_time_s * 100, 1)


def _build_candidate(
    profile: AthleteSegmentProfile,
    enrichment: SegmentEnrichment | None,
    home_lat: float | None,
    home_lng: float | None,
) -> SegmentCandidate:
    kom_time_s = enrichment.kom_time_s if enrichment else None
    gap_to_kom_s, gap_to_kom_pct = _gap_to_kom(profile, enrichment)
    seg_name = (
        (enrichment.segment_name if enrichment and enrichment.segment_name else None)
        or profile.segment_name
        or f"Segment {profile.segment_id}"
    )
    return SegmentCandidate(
        segment_id=profile.segment_id,
        segment_name=seg_name,
        top10_seen=profile.top10_seen,
        podium_seen=profile.podium_seen,
        best_seen_kom_rank=profile.best_seen_kom_rank,
        last_seen_kom_rank=profile.last_seen_kom_rank,
        best_time_s=profile.best_time_s,
        best_time_display=seconds_to_display(profile.best_time_s),
        latest_time_s=profile.latest_time_s,
        latest_time_display=seconds_to_display(profile.latest_time_s),
        times_ridden=profile.times_ridden,
        best_avg_watts=profile.best_avg_watts,
        latest_avg_watts=profile.latest_avg_watts,
        last_ridden_at=profile.last_ridden_at,
        kom_time_s=kom_time_s,
        kom_time_display=seconds_to_display(kom_time_s),
        gap_to_kom_s=gap_to_kom_s,
        gap_to_kom_display=seconds_to_display(gap_to_kom_s),
        gap_to_kom_pct=gap_to_kom_pct,
        average_grade=enrichment.avg_grade_pct if enrichment else None,
        distance_m=enrichment.distance_m if enrichment else None,
        distance_from_home_km=_distance_from_home(enrichment, home_lat, home_lng),
        is_indoor=profile.is_indoor,
        city=enrichment.city if enrichment else None,
        country=enrichment.country if enrichment else None,
        climb_category=enrichment.climb_category if enrichment else None,
        segment_url=f"https://www.strava.com/segments/{profile.segment_id}",
    )


async def get_candidates(
    db: AsyncSession,
    athlete_id: int,
    filters: CandidateFilters,
) -> CandidatesResponse:
    # Resolve home location: athlete's saved coords take priority over env defaults
    profile_result = await db.execute(
        select(AthleteProfile).where(AthleteProfile.athlete_id == athlete_id)
    )
    athlete_profile = profile_result.scalar_one_or_none()
    home_lat = (
        athlete_profile.home_lat
        if athlete_profile and athlete_profile.home_lat is not None
        else settings.home_lat
    )
    home_lng = (
        athlete_profile.home_lng
        if athlete_profile and athlete_profile.home_lng is not None
        else settings.home_lng
    )

    stmt = (
        select(AthleteSegmentProfile, SegmentEnrichment)
        .outerjoin(
            SegmentEnrichment,
            AthleteSegmentProfile.segment_id == SegmentEnrichment.segment_id,
        )
        .where(
            AthleteSegmentProfile.athlete_id == athlete_id,
            AthleteSegmentProfile.is_starred == True,
            AthleteSegmentProfile.times_ridden > 0,
        )
    )
    stmt = _apply_filters(stmt, filters)

    result = await db.execute(stmt)
    candidates = [
        _build_candidate(profile, enrichment, home_lat, home_lng)
        for profile, enrichment in result.all()
    ]
    candidates.sort(key=lambda c: (c.distance_from_home_km is None, c.distance_from_home_km or 0))

    return CandidatesResponse(
        fetched_at=datetime.now(timezone.utc),
        total=len(candidates),
        candidates=candidates,
    )
