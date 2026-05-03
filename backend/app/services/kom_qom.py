import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.athlete import AthleteProfile, AthleteZones
from app.models.segment import AthleteSegmentProfile, SegmentEffortBackfillState, SegmentEnrichment
from app.schemas.kom_qom import CandidateFilters, CandidatesResponse, SegmentCandidate
from app.utils import haversine_km, seconds_to_display, xom_label_for_sex


@dataclass(frozen=True)
class _PowerZone:
    index: int
    min_watts: float
    max_watts: float | None


@dataclass(frozen=True)
class _PowerDifficulty:
    best_power_zone: int | None
    estimated_kom_power_watts: float | None
    estimated_kom_power_zone: int | None
    kom_difficulty: str | None
    kom_difficulty_label: str | None


def _apply_filters(stmt, filters: CandidateFilters):
    known_time_s = func.coalesce(AthleteSegmentProfile.best_time_s, AthleteSegmentProfile.pr_time_s)
    if filters.podium_only:
        stmt = stmt.where(AthleteSegmentProfile.podium_seen == True)
    if filters.effort_time_min is not None:
        stmt = stmt.where(known_time_s >= filters.effort_time_min)
    if filters.effort_time_max is not None:
        stmt = stmt.where(known_time_s <= filters.effort_time_max)
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


def _target_time_s(
    profile: AthleteSegmentProfile,
    enrichment: SegmentEnrichment | None,
    xom_label: str,
) -> int | None:
    if enrichment:
        if xom_label == "QOM":
            if enrichment.qom_time_s is not None:
                return enrichment.qom_time_s
        elif enrichment.kom_time_s is not None:
            return enrichment.kom_time_s
    if profile.is_kom and profile.pr_time_s is not None:
        return profile.pr_time_s
    return None


def _gap_to_xom(
    profile: AthleteSegmentProfile,
    enrichment: SegmentEnrichment | None,
    xom_label: str,
) -> tuple[int | None, float | None]:
    target_time_s = _target_time_s(profile, enrichment, xom_label)
    known_time_s = profile.best_time_s or profile.pr_time_s
    if not target_time_s or not known_time_s:
        return None, None
    gap_s = 0 if profile.is_kom and profile.pr_time_s else known_time_s - target_time_s
    return gap_s, round(gap_s / target_time_s * 100, 1)


def _is_current_xom(
    profile: AthleteSegmentProfile,
    gap_to_kom_s: int | None,
) -> bool:
    return profile.is_kom or gap_to_kom_s == 0


def _data_quality(
    profile: AthleteSegmentProfile,
    enrichment: SegmentEnrichment | None,
    backfill_state: SegmentEffortBackfillState | None,
    xom_label: str,
) -> str:
    if backfill_state and backfill_state.status == "done":
        return "backfilled"
    if _target_time_s(profile, enrichment, xom_label) is not None:
        return "enriched"
    if profile.times_ridden > 0:
        return "imported"
    return "seeded"


def _parse_power_zones(athlete_zones: AthleteZones | None) -> list[_PowerZone]:
    if athlete_zones is None:
        return []
    try:
        payload = json.loads(athlete_zones.zones_json)
    except (TypeError, json.JSONDecodeError):
        return []

    power = payload.get("power") if isinstance(payload, dict) else None
    raw_zones = power.get("zones") if isinstance(power, dict) else None
    if not isinstance(raw_zones, list):
        return []

    zones: list[_PowerZone] = []
    for index, raw_zone in enumerate(raw_zones, start=1):
        if not isinstance(raw_zone, dict):
            continue
        min_watts = raw_zone.get("min")
        max_watts = raw_zone.get("max")
        if min_watts is None:
            continue
        zones.append(
            _PowerZone(
                index=index,
                min_watts=float(min_watts),
                max_watts=None if max_watts in (None, -1) else float(max_watts),
            )
        )
    return zones


def _zone_for_power(power_watts: float | None, zones: list[_PowerZone]) -> int | None:
    if power_watts is None or not zones:
        return None
    rounded_power = round(power_watts)
    for zone in zones:
        if rounded_power >= zone.min_watts and (
            zone.max_watts is None or rounded_power <= zone.max_watts
        ):
            return zone.index
    if rounded_power < zones[0].min_watts:
        return zones[0].index
    if zones[-1].max_watts is not None and rounded_power > zones[-1].max_watts:
        return zones[-1].index
    return None


def _difficulty_label(difficulty: str | None, xom_label: str) -> str | None:
    labels = {
        "easy": "Easy to get",
        "realistic": "Realistic",
        "hard": f"Hard to {xom_label}",
    }
    return labels.get(difficulty or "")


def _power_difficulty(
    profile: AthleteSegmentProfile,
    enrichment: SegmentEnrichment | None,
    zones: list[_PowerZone],
    xom_label: str,
) -> _PowerDifficulty:
    best_power_zone = _zone_for_power(profile.best_avg_watts, zones)
    known_time_s = profile.best_time_s or profile.pr_time_s
    target_time_s = _target_time_s(profile, enrichment, xom_label)
    gap_to_kom_s, gap_to_kom_pct = _gap_to_xom(profile, enrichment, xom_label)

    if not zones or profile.best_avg_watts is None or not known_time_s or not target_time_s:
        return _PowerDifficulty(best_power_zone, None, None, None, None)
    if gap_to_kom_s is None or gap_to_kom_s <= 0 or gap_to_kom_pct is None:
        return _PowerDifficulty(best_power_zone, None, None, None, None)

    estimated_power = round(profile.best_avg_watts * known_time_s / target_time_s, 1)
    estimated_zone = _zone_for_power(estimated_power, zones)

    difficulty = None
    if estimated_zone is not None:
        if estimated_zone <= 4 and gap_to_kom_pct <= 5:
            difficulty = "easy"
        elif estimated_zone <= 5 and gap_to_kom_pct <= 15:
            difficulty = "realistic"
        else:
            difficulty = "hard"

    return _PowerDifficulty(
        best_power_zone=best_power_zone,
        estimated_kom_power_watts=estimated_power,
        estimated_kom_power_zone=estimated_zone,
        kom_difficulty=difficulty,
        kom_difficulty_label=_difficulty_label(difficulty, xom_label),
    )


def _build_candidate(
    profile: AthleteSegmentProfile,
    enrichment: SegmentEnrichment | None,
    backfill_state: SegmentEffortBackfillState | None,
    home_lat: float | None,
    home_lng: float | None,
    power_zones: list[_PowerZone],
    xom_label: str,
) -> SegmentCandidate:
    kom_time_s = _target_time_s(profile, enrichment, xom_label)
    gap_to_kom_s, gap_to_kom_pct = _gap_to_xom(profile, enrichment, xom_label)
    power_difficulty = _power_difficulty(profile, enrichment, power_zones, xom_label)
    seg_name = (
        (enrichment.segment_name if enrichment and enrichment.segment_name else None)
        or profile.segment_name
        or f"Segment {profile.segment_id}"
    )
    e = enrichment
    return SegmentCandidate(
        segment_id=profile.segment_id,
        segment_name=seg_name,
        top10_seen=profile.top10_seen,
        podium_seen=profile.podium_seen,
        best_seen_kom_rank=profile.best_seen_kom_rank,
        last_seen_kom_rank=profile.last_seen_kom_rank,
        is_kom=_is_current_xom(profile, gap_to_kom_s),
        xom_label=xom_label,
        data_quality=_data_quality(profile, enrichment, backfill_state, xom_label),
        best_time_s=profile.best_time_s,
        best_time_display=seconds_to_display(profile.best_time_s),
        latest_time_s=profile.latest_time_s,
        latest_time_display=seconds_to_display(profile.latest_time_s),
        pr_time_s=profile.pr_time_s,
        pr_time_display=seconds_to_display(profile.pr_time_s),
        pr_date=profile.pr_date,
        times_ridden=profile.times_ridden,
        best_avg_watts=profile.best_avg_watts,
        latest_avg_watts=profile.latest_avg_watts,
        best_power_zone=power_difficulty.best_power_zone,
        estimated_kom_power_watts=power_difficulty.estimated_kom_power_watts,
        estimated_kom_power_zone=power_difficulty.estimated_kom_power_zone,
        kom_difficulty=power_difficulty.kom_difficulty,
        kom_difficulty_label=power_difficulty.kom_difficulty_label,
        last_ridden_at=profile.last_ridden_at,
        starred_date=profile.starred_date,
        kom_time_s=kom_time_s,
        kom_time_display=seconds_to_display(kom_time_s),
        gap_to_kom_s=gap_to_kom_s,
        gap_to_kom_display=seconds_to_display(gap_to_kom_s),
        gap_to_kom_pct=gap_to_kom_pct,
        average_grade=e.avg_grade_pct if e else None,
        distance_m=e.distance_m if e else None,
        elevation_high=e.elevation_high if e else None,
        elevation_low=e.elevation_low if e else None,
        distance_from_home_km=_distance_from_home(enrichment, home_lat, home_lng),
        is_indoor=profile.is_indoor,
        activity_type=e.activity_type if e else None,
        hazardous=e.hazardous if e else None,
        city=e.city if e else None,
        state=e.state if e else None,
        country=e.country if e else None,
        climb_category=e.climb_category if e else None,
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
    xom_label = xom_label_for_sex(athlete_profile.sex if athlete_profile else None)
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
    zones_result = await db.execute(
        select(AthleteZones).where(AthleteZones.athlete_id == athlete_id)
    )
    power_zones = _parse_power_zones(zones_result.scalar_one_or_none())

    stmt = (
        select(AthleteSegmentProfile, SegmentEnrichment, SegmentEffortBackfillState)
        .outerjoin(
            SegmentEnrichment,
            AthleteSegmentProfile.segment_id == SegmentEnrichment.segment_id,
        )
        .outerjoin(
            SegmentEffortBackfillState,
            and_(
                SegmentEffortBackfillState.athlete_id == AthleteSegmentProfile.athlete_id,
                SegmentEffortBackfillState.segment_id == AthleteSegmentProfile.segment_id,
            ),
        )
        .where(
            AthleteSegmentProfile.athlete_id == athlete_id,
            AthleteSegmentProfile.is_starred == True,
            or_(
                AthleteSegmentProfile.times_ridden > 0,
                AthleteSegmentProfile.pr_time_s.is_not(None),
            ),
        )
    )
    stmt = _apply_filters(stmt, filters)

    result = await db.execute(stmt)
    candidates = [
        _build_candidate(
            profile,
            enrichment,
            backfill_state,
            home_lat,
            home_lng,
            power_zones,
            xom_label,
        )
        for profile, enrichment, backfill_state in result.all()
    ]
    candidates.sort(key=lambda c: (
        not c.is_kom,
        c.distance_from_home_km is None,
        c.distance_from_home_km or 0,
    ))

    return CandidatesResponse(
        fetched_at=datetime.now(timezone.utc),
        xom_label=xom_label,
        total=len(candidates),
        candidates=candidates,
    )
