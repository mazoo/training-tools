# Architecture

## System overview

```
Browser (Astro MPA)
        │  REST JSON  (/api/*)
        ▼
FastAPI backend  ──────────────────────── SQLite DB
        │                                  (segment profiles, effort digests,
        │  HTTPS + OAuth token             enrichment cache, tokens)
        ▼
Strava API  (api.strava.com/v3)
```

The backend is the only component that ever calls Strava. The frontend talks only to the backend. This keeps the Strava OAuth tokens server-side and makes rate-limit enforcement trivial (single process).

## Two-layer data model

The data model mirrors the proven approach from the companion `attack-selector` project.

**Layer 1 — Activity import (primary, reliable)**

`GET /activities/{id}` returns `segment_efforts[]`. Each effort carries `kom_rank` (null if the athlete was outside top 10 at the time), `average_watts`, `elapsed_time`, and `moving_time`. These are stored verbatim in `segment_effort_digest` and then aggregated into `athlete_segment_profile`.

`top10_seen` and `podium_seen` are derived here and are the primary signals for KOM/QOM candidate identification. They do not depend on the leaderboard endpoint (which Strava removed).

**Layer 2 — Segment enrichment (optional)**

`GET /segments/{id}` provides geometry (`start_latlng`, `avg_grade_pct`), city/country, and potentially `xoms.kom` (KOM time string). `xoms` availability is uncertain — treat `kom_time_s` and `gap_to_kom_s` as best-effort enrichment that may be null. The candidate list is fully functional without it.

## Data flow: KOM/QOM candidates

### Bootstrap (first connect)

```
1. Sync starred segments  (GET /athlete/segments/starred, paginated)
   └─ store segment IDs + basic fields

2. Sync recent activities  (GET /athlete/activities, bounded window)
   └─ for each activity: GET /activities/{id}
      └─ extract segment_efforts[]
         → insert into segment_effort_digest
         → recompute athlete_segment_profile (times_ridden, best_time_s,
           best_avg_watts, top10_seen, podium_seen, best_seen_kom_rank)

3. Enrichment (background, rate-limited)
   └─ for starred segments only: GET /segments/{id}
      → store geometry + optional xoms data in segment_enrichment
```

### Query path (hot, no Strava calls)

```
GET /api/kom-qom/candidates
        │
        ├─ 1. Read athlete_segment_profile WHERE is_starred = true
        │      AND (top10_seen = true OR podium_seen = true)
        │
        ├─ 2. JOIN segment_enrichment  (for grade, geometry, xoms gap)
        │      Compute distance_from_home_km = haversine(start_lat, start_lng, HOME_LAT, HOME_LNG)
        │      (cheap calculation; not stored — home location can change anytime)
        │
        ├─ 3. Apply filters
        │      effort_time_min / effort_time_max  → best_time_s
        │      gradient_min / gradient_max        → avg_grade_pct
        │      surface                            → is_indoor
        │      podium_only                        → podium_seen = true
        │
        └─ 4. Sort by distance_from_home ASC (nulls last), return JSON
```

## Caching / sync strategy

| Data | Source | Update trigger |
|------|--------|---------------|
| Starred segments | `GET /athlete/segments/starred` | Manual refresh or bootstrap |
| Activity list | `GET /athlete/activities` | Incremental (newest-first, stop at last known) |
| Activity details | `GET /activities/{id}` | Once per activity; re-fetch on webhook `update` |
| Segment enrichment | `GET /segments/{id}` | TTL 24 h; only starred segments |

On a full cold-start for an athlete with 100 starred segments and 200 recent activities:

- 1 call: starred segments
- ~200 calls: activity details (spread over multiple 15-min windows as needed)
- up to 100 calls: segment enrichment (background, non-blocking)

The query path reads only local DB — no Strava calls during a page load.

## Database schema

```sql
-- OAuth tokens per athlete
CREATE TABLE athlete_tokens (
    athlete_id      INTEGER PRIMARY KEY,
    access_token    TEXT NOT NULL,
    refresh_token   TEXT NOT NULL,
    expires_at      INTEGER NOT NULL   -- Unix timestamp
);

-- One row per segment effort extracted from a detailed activity.
-- Source: GET /activities/{id} → segment_efforts[]
CREATE TABLE segment_effort_digest (
    effort_id       INTEGER PRIMARY KEY,   -- Strava segment_effort.id
    athlete_id      INTEGER NOT NULL,
    segment_id      INTEGER NOT NULL,
    activity_id     INTEGER NOT NULL,
    effort_date     DATE NOT NULL,
    elapsed_s       INTEGER NOT NULL,
    moving_s        INTEGER,
    avg_watts       REAL,                  -- null if no power meter
    kom_rank        INTEGER,               -- rank at time of effort; null if outside top 10
    pr_rank         INTEGER                -- 1 if PR at time of effort, else null
);

-- Aggregated profile per athlete+segment. Recomputed from effort_digest rows.
CREATE TABLE athlete_segment_profile (
    athlete_id              INTEGER NOT NULL,
    segment_id              INTEGER NOT NULL,
    segment_name            TEXT NOT NULL,
    is_starred              BOOLEAN NOT NULL DEFAULT 0,
    is_indoor               BOOLEAN NOT NULL DEFAULT 0,
    times_ridden            INTEGER NOT NULL DEFAULT 0,
    best_time_s             INTEGER,       -- min(elapsed_s) across all efforts
    latest_time_s           INTEGER,       -- elapsed_s of most recent effort
    best_avg_watts          REAL,          -- max(avg_watts) across all efforts
    latest_avg_watts        REAL,          -- avg_watts of most recent effort
    top10_seen              BOOLEAN NOT NULL DEFAULT 0,  -- any effort had kom_rank 1–10
    podium_seen             BOOLEAN NOT NULL DEFAULT 0,  -- any effort had kom_rank 1–3
    best_seen_kom_rank      INTEGER,       -- min(kom_rank) ever seen (1 = held KOM)
    last_seen_kom_rank      INTEGER,       -- kom_rank of most recent effort (null if >10)
    last_ridden_at          DATETIME,
    updated_at              DATETIME NOT NULL,
    PRIMARY KEY (athlete_id, segment_id)
);

-- Optional enrichment from GET /segments/{id}. Starred segments only.
-- kom_time_s and gap_to_kom_s may be null if xoms is unavailable.
CREATE TABLE segment_enrichment (
    segment_id              INTEGER PRIMARY KEY,
    distance_m              REAL,
    avg_grade_pct           REAL,
    max_grade_pct           REAL,
    start_lat               REAL,
    start_lng               REAL,
    city                    TEXT,
    country                 TEXT,
    climb_category          INTEGER,
    kom_time_s              INTEGER,       -- parsed from xoms.kom; null if unavailable
    gap_to_kom_s            INTEGER,       -- best_time_s - kom_time_s; null if kom_time_s null
    cached_at               DATETIME NOT NULL
);
```

## Rate-limit error handling

If the Strava API returns `429 Too Many Requests`, the client:
1. Reads the `X-RateLimit-Limit` / `X-RateLimit-Usage` headers.
2. Calculates the retry-after time (next 15-min window boundary).
3. Raises `StravaRateLimitError` which is caught in the router and returned as `HTTP 429` with a `retry_after` field so the frontend can display a countdown.

## Adding a new tool

1. Create `docs/features/MY_TOOL.md` with the feature spec.
2. Add ORM models to `backend/app/models/`.
3. Add a router at `backend/app/routers/my_tool.py`.
4. Register the router in `backend/app/main.py`.
5. Add a page/component in `frontend/src/pages/`.
6. Write tests.
