from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.athlete import AthleteSyncState, AthleteToken
from app.models.segment import (
    AthleteSegmentProfile,
    SegmentEffortBackfillState,
    SegmentEffortDigest,
    SegmentEnrichment,
)
from app.schemas.kom_qom import CandidateFilters
from app.services import sync
from app.services.kom_qom import get_candidates
from app.tasks import TaskStatus


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


def _starred_segment(
    segment_id: int,
    *,
    pr_time_s: int = 60,
    is_kom: bool = False,
    activity_type: str = "Ride",
    average_grade: float = 5.0,
    starred_date: str = "2026-04-01T10:00:00Z",
) -> dict:
    return {
        "id": segment_id,
        "name": f"Segment {segment_id}",
        "distance": 1000.0,
        "average_grade": average_grade,
        "maximum_grade": average_grade + 2,
        "start_latlng": [47.0, 7.0],
        "end_latlng": [47.01, 7.01],
        "city": "Solothurn",
        "country": "Switzerland",
        "state": None,
        "climb_category": 0,
        "elevation_high": 500.0,
        "elevation_low": 450.0,
        "activity_type": activity_type,
        "hazardous": False,
        "starred_date": starred_date,
        "athlete_pr_effort": {
            "id": segment_id * 1000,
            "activity_id": segment_id * 10,
            "elapsed_time": pr_time_s,
            "start_date": "2026-03-20T10:00:00Z",
            "is_kom": is_kom,
        },
    }


def _fake_client_class(pages: list[list[dict]], segment_response: dict | None = None):
    class FakeStravaClient:
        instances: list["FakeStravaClient"] = []

        def __init__(self, access_token: str) -> None:
            self.access_token = access_token
            self.segment_calls: list[int] = []
            FakeStravaClient.instances.append(self)

        async def get_starred_segments_page(self, page: int = 1, per_page: int = 200) -> list[dict]:
            return pages[page - 1] if page - 1 < len(pages) else []

        async def get_segment(self, segment_id: int) -> dict:
            self.segment_calls.append(segment_id)
            return segment_response or {"xoms": {"kom": "0:50"}}

    return FakeStravaClient


@pytest.mark.asyncio
async def test_onboarding_never_exceeds_150_calls(db_session, monkeypatch):
    pages = [
        [_starred_segment(i, pr_time_s=100 + i) for i in range(1, 201)],
        [_starred_segment(i, pr_time_s=100 + i) for i in range(201, 251)],
    ]
    fake_client = _fake_client_class(pages)
    monkeypatch.setattr(sync, "StravaClient", fake_client)

    task = TaskStatus(task_id="bootstrap")
    await sync.run_sync(db_session, athlete_id=1, access_token="token", task=task)

    client = fake_client.instances[0]
    assert task.status == "done"
    assert task.strava_calls_made == sync.ONBOARDING_STRAVA_CALL_BUDGET
    assert len(client.segment_calls) == sync.ONBOARDING_STRAVA_CALL_BUDGET - 2


@pytest.mark.asyncio
async def test_current_kom_gets_zero_gap_without_detail_call(db_session, monkeypatch):
    pages = [[_starred_segment(10, pr_time_s=55, is_kom=True)]]
    fake_client = _fake_client_class(pages)
    monkeypatch.setattr(sync, "StravaClient", fake_client)

    task = TaskStatus(task_id="bootstrap")
    await sync.run_sync(db_session, athlete_id=1, access_token="token", task=task)

    client = fake_client.instances[0]
    assert client.segment_calls == []

    response = await get_candidates(db_session, 1, CandidateFilters())
    assert response.total == 1
    candidate = response.candidates[0]
    assert candidate.times_ridden == 0
    assert candidate.pr_time_s == 55
    assert candidate.kom_time_s == 55
    assert candidate.gap_to_kom_s == 0
    assert candidate.data_quality == "enriched"


@pytest.mark.asyncio
async def test_best_chance_prioritization_is_deterministic(db_session):
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    db_session.add_all(
        [
            AthleteSegmentProfile(
                athlete_id=1,
                segment_id=30,
                segment_name="Outdoor B",
                is_starred=True,
                is_indoor=False,
                times_ridden=0,
                top10_seen=False,
                podium_seen=False,
                pr_time_s=600,
                pr_date=now,
                updated_at=now,
            ),
            SegmentEnrichment(
                segment_id=30,
                segment_name="Outdoor B",
                avg_grade_pct=5.0,
                activity_type="Ride",
                cached_at=now,
            ),
            AthleteSegmentProfile(
                athlete_id=1,
                segment_id=20,
                segment_name="Outdoor A",
                is_starred=True,
                is_indoor=False,
                times_ridden=0,
                top10_seen=False,
                podium_seen=False,
                pr_time_s=600,
                pr_date=now,
                updated_at=now,
            ),
            SegmentEnrichment(
                segment_id=20,
                segment_name="Outdoor A",
                avg_grade_pct=5.0,
                activity_type="Ride",
                cached_at=now,
            ),
            AthleteSegmentProfile(
                athlete_id=1,
                segment_id=10,
                segment_name="Indoor",
                is_starred=True,
                is_indoor=True,
                times_ridden=0,
                top10_seen=False,
                podium_seen=False,
                pr_time_s=60,
                pr_date=now,
                updated_at=now,
            ),
            SegmentEnrichment(
                segment_id=10,
                segment_name="Indoor",
                avg_grade_pct=4.0,
                activity_type="VirtualRide",
                cached_at=now,
            ),
        ]
    )
    await db_session.commit()

    segment_ids = await sync._get_pending_kom_time_backfill_ids(db_session, 1)
    assert segment_ids == [20, 30, 10]


@pytest.mark.asyncio
async def test_stale_empty_pr_seeded_backfill_is_retried(db_session):
    star_sync_at = datetime(2026, 4, 29, tzinfo=timezone.utc)
    stale_done_at = datetime(2026, 4, 28, tzinfo=timezone.utc)
    fresh_done_at = datetime(2026, 4, 30, tzinfo=timezone.utc)
    db_session.add_all(
        [
            AthleteSyncState(
                athlete_id=1,
                bootstrap_done=True,
                last_star_sync_at=star_sync_at,
                backfill_complete=False,
            ),
            AthleteSegmentProfile(
                athlete_id=1,
                segment_id=7914618,
                segment_name="Aarwangenstrasse Climb",
                is_starred=True,
                is_indoor=False,
                times_ridden=0,
                top10_seen=False,
                podium_seen=False,
                pr_time_s=167,
                updated_at=star_sync_at,
            ),
            SegmentEnrichment(
                segment_id=7914618,
                segment_name="Aarwangenstrasse Climb",
                avg_grade_pct=1.3,
                activity_type="Ride",
                kom_time_s=160,
                cached_at=star_sync_at,
            ),
            SegmentEffortBackfillState(
                athlete_id=1,
                segment_id=7914618,
                status="done",
                completed_at=stale_done_at,
                last_attempt_at=stale_done_at,
            ),
            AthleteSegmentProfile(
                athlete_id=1,
                segment_id=20,
                segment_name="Already Imported",
                is_starred=True,
                is_indoor=False,
                times_ridden=1,
                top10_seen=False,
                podium_seen=False,
                pr_time_s=100,
                updated_at=star_sync_at,
            ),
            SegmentEffortBackfillState(
                athlete_id=1,
                segment_id=20,
                status="done",
                completed_at=stale_done_at,
                last_attempt_at=stale_done_at,
            ),
            SegmentEffortDigest(
                effort_id=2000,
                athlete_id=1,
                segment_id=20,
                activity_id=200,
                effort_date=star_sync_at.date(),
                elapsed_s=100,
            ),
            AthleteSegmentProfile(
                athlete_id=1,
                segment_id=30,
                segment_name="Fresh Empty",
                is_starred=True,
                is_indoor=False,
                times_ridden=0,
                top10_seen=False,
                podium_seen=False,
                pr_time_s=120,
                updated_at=star_sync_at,
            ),
            SegmentEffortBackfillState(
                athlete_id=1,
                segment_id=30,
                status="done",
                completed_at=fresh_done_at,
                last_attempt_at=fresh_done_at,
            ),
        ]
    )
    await db_session.commit()

    segment_ids = await sync._get_pending_segment_effort_backfill_ids(db_session, 1)
    assert segment_ids == [7914618]


@pytest.mark.asyncio
async def test_backfill_chunk_retries_stale_empty_when_complete(db_session, monkeypatch):
    star_sync_at = datetime(2026, 4, 29, tzinfo=timezone.utc)
    stale_done_at = datetime(2026, 4, 28, tzinfo=timezone.utc)
    db_session.add_all(
        [
            AthleteSyncState(
                athlete_id=1,
                bootstrap_done=True,
                last_star_sync_at=star_sync_at,
                backfill_complete=True,
            ),
            AthleteSegmentProfile(
                athlete_id=1,
                segment_id=7914618,
                segment_name="Aarwangenstrasse Climb",
                is_starred=True,
                is_indoor=False,
                times_ridden=0,
                top10_seen=False,
                podium_seen=False,
                pr_time_s=167,
                updated_at=star_sync_at,
            ),
            SegmentEnrichment(
                segment_id=7914618,
                segment_name="Aarwangenstrasse Climb",
                avg_grade_pct=1.3,
                activity_type="Ride",
                kom_time_s=160,
                kom_time_checked_at=star_sync_at,
                cached_at=star_sync_at,
            ),
            SegmentEffortBackfillState(
                athlete_id=1,
                segment_id=7914618,
                status="done",
                completed_at=stale_done_at,
                last_attempt_at=stale_done_at,
            ),
        ]
    )
    await db_session.commit()

    class FakeStravaClient:
        def __init__(self, access_token: str) -> None:
            self.access_token = access_token

        async def get_segment_efforts(
            self,
            segment_id: int,
            per_page: int = 200,
            page: int = 1,
        ) -> list[dict]:
            assert segment_id == 7914618
            if page > 1:
                return []
            return [
                {
                    "id": 3265372731392868352,
                    "activity": {"id": 12300336835},
                    "elapsed_time": 167,
                    "moving_time": 167,
                    "average_watts": 295.8,
                    "kom_rank": 5,
                    "pr_rank": None,
                    "start_date": "2024-09-01T16:20:12Z",
                    "segment": {
                        "id": 7914618,
                        "name": "Aarwangenstrasse Climb",
                        "activity_type": "Ride",
                        "distance": 1764.7,
                        "average_grade": 1.3,
                        "maximum_grade": 6.3,
                        "start_latlng": [47.259465, 7.707584],
                        "city": "Niederbipp",
                        "country": "Switzerland",
                        "climb_category": 0,
                    },
                }
            ]

    monkeypatch.setattr(sync, "StravaClient", FakeStravaClient)

    task = TaskStatus(task_id="backfill")
    await sync.run_backfill_chunk(db_session, athlete_id=1, access_token="token", task=task)

    response = await get_candidates(db_session, 1, CandidateFilters())
    candidate = response.candidates[0]
    assert task.status == "done"
    assert task.strava_calls_made == 1
    assert candidate.segment_id == 7914618
    assert candidate.times_ridden == 1
    assert candidate.best_time_s == 167
    assert candidate.best_avg_watts == 295.8
    assert candidate.top10_seen is True
    assert candidate.podium_seen is False


@pytest.mark.asyncio
async def test_daily_backfill_uses_10_call_round_robin_budget(db_session, monkeypatch):
    sync._BACKFILL_ROUND_ROBIN_INDEX = 0
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    db_session.add_all(
        [
            AthleteToken(athlete_id=1, access_token="old", refresh_token="refresh", expires_at=9999999999),
            AthleteToken(athlete_id=2, access_token="old", refresh_token="refresh", expires_at=9999999999),
            AthleteSyncState(athlete_id=1, bootstrap_done=True, last_star_sync_at=now, backfill_complete=False),
            AthleteSyncState(athlete_id=2, bootstrap_done=True, last_star_sync_at=now, backfill_complete=False),
        ]
    )
    await db_session.commit()

    calls: list[tuple[int, int | None]] = []

    async def fake_get_valid_access_token(athlete_id, db):
        return f"token-{athlete_id}"

    async def fake_run_backfill_chunk(db, athlete_id, access_token, task, call_budget=None):
        calls.append((athlete_id, call_budget))
        task.strava_calls_made = call_budget or 0
        task.status = "done"

    import app.services.auth as auth_service

    monkeypatch.setattr(auth_service, "get_valid_access_token", fake_get_valid_access_token)
    monkeypatch.setattr(sync, "run_backfill_chunk", fake_run_backfill_chunk)

    first = await sync.run_daily_backfill(db_session)
    second = await sync.run_daily_backfill(db_session)

    assert calls == [(1, sync.BACKFILL_CALLS_PER_15MIN_WINDOW), (2, sync.BACKFILL_CALLS_PER_15MIN_WINDOW)]
    assert first[1] == "done"
    assert first[2] == "budget_deferred"
    assert second[2] == "done"
