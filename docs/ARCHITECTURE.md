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

**Layer 1 — Activity import (primary, reliable)**

`GET /activities/{id}` returns `segment_efforts[]`. Each effort carries `kom_rank` (null if the athlete was outside top 10 at the time), `average_watts`, `elapsed_time`, and `moving_time`. These are stored verbatim in `segment_effort_digest` and then aggregated into `athlete_segment_profile`.

`top10_seen` and `podium_seen` are derived here and are the primary signals for KOM/QOM candidate identification. They do not depend on the leaderboard endpoint (which Strava removed).

**Layer 2 — Segment enrichment (optional)**

`GET /segments/{id}` provides geometry (`start_latlng`, `avg_grade_pct`), city/country, and potentially `xoms.kom` (KOM time string). `xoms` availability is uncertain — treat `kom_time_s` and the gap-to-KOM as best-effort enrichment that may be null. The candidate list is fully functional without it.

`gap_to_kom_s` is **not stored** — it is computed at query time as `best_time_s - kom_time_s` in the service layer. This avoids a stale derived column and lets home-distance calculation (also query-time) stay consistent.

## Data flow: KOM/QOM candidates

### Bootstrap (first connect)

```
1. Sync starred segments  (GET /athlete/segments/starred, paginated)
   └─ store segment IDs + basic fields in athlete_segment_profile

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
        ├─ 2. JOIN segment_enrichment  (for grade, geometry, xoms)
        │      Compute distance_from_home_km = haversine(start_lat, start_lng, home_lat, home_lng)
        │      Compute gap_to_kom_s = best_time_s - kom_time_s  (null if kom_time_s null)
        │      (both are cheap calculations; neither is stored)
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

| Data | Source | Update trigger | TTL |
|------|--------|----------------|-----|
| Starred segments | `GET /athlete/segments/starred` | Manual refresh or bootstrap | — |
| Activity list | `GET /athlete/activities` | Incremental (newest-first, stop at last known) | — |
| Activity details | `GET /activities/{id}` | Once per activity | — |
| Segment enrichment | `GET /segments/{id}` | Stale after **7 days**; starred segments only | 7 days |

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

-- Cached Strava profile + per-athlete settings
CREATE TABLE athlete_profile (
    athlete_id      INTEGER PRIMARY KEY,
    firstname       TEXT,
    lastname        TEXT,
    profile_medium  TEXT,              -- Strava avatar URL (medium size)
    home_address    TEXT,              -- set via Nominatim geocoding
    home_lat        REAL,              -- per-athlete override; null = use HOME_LAT env var
    home_lng        REAL
);

-- Sync progress per athlete
CREATE TABLE athlete_sync_state (
    athlete_id              INTEGER PRIMARY KEY,
    bootstrap_done          BOOLEAN DEFAULT 0,  -- true after the first run_sync completes
    last_activity_sync_at   DATETIME,           -- updated after each run_sync
    last_star_sync_at       DATETIME,           -- updated after each starred-segment fetch
    backfill_cursor_at      DATETIME,           -- how far back the historical backfill has reached
    backfill_complete       BOOLEAN DEFAULT 0
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
    pr_rank         INTEGER
);

-- Aggregated profile per athlete+segment. Recomputed from effort_digest rows.
CREATE TABLE athlete_segment_profile (
    athlete_id              INTEGER NOT NULL,
    segment_id              INTEGER NOT NULL,
    segment_name            TEXT NOT NULL,
    is_starred              BOOLEAN NOT NULL DEFAULT 0,
    is_indoor               BOOLEAN NOT NULL DEFAULT 0,
    times_ridden            INTEGER NOT NULL DEFAULT 0,
    best_time_s             INTEGER,
    latest_time_s           INTEGER,
    best_avg_watts          REAL,
    latest_avg_watts        REAL,
    top10_seen              BOOLEAN NOT NULL DEFAULT 0,
    podium_seen             BOOLEAN NOT NULL DEFAULT 0,
    best_seen_kom_rank      INTEGER,
    last_seen_kom_rank      INTEGER,
    last_ridden_at          DATETIME,
    updated_at              DATETIME NOT NULL,
    PRIMARY KEY (athlete_id, segment_id)
);

-- Optional enrichment from GET /segments/{id}. Starred segments only. TTL = 7 days.
-- gap_to_kom_s is NOT stored here — it is computed at query time (best_time_s - kom_time_s).
CREATE TABLE segment_enrichment (
    segment_id              INTEGER PRIMARY KEY,
    segment_name            TEXT,
    distance_m              REAL,
    avg_grade_pct           REAL,
    max_grade_pct           REAL,
    start_lat               REAL,
    start_lng               REAL,
    city                    TEXT,
    country                 TEXT,
    climb_category          INTEGER,
    kom_time_s              INTEGER,       -- parsed from xoms.kom; null if unavailable
    cached_at               DATETIME NOT NULL
);
```

## Rate-limit strategy

The rate limiter (`strava/rate_limiter.py`) is **proactive**, not reactive. It maintains an in-process token-bucket and raises `BudgetExhausted` *before* making an HTTP call when headroom thresholds are breached:

- 15-min window: raises when `remaining < 20` (out of 200)
- Daily window: raises when `remaining < 100` (out of 2000)

After each Strava response the limiter syncs its counters from `X-RateLimit-Usage` / `X-RateLimit-Limit` headers (ground truth) and persists state to `rate_limit_state.json` so budget survives process restarts.

Receiving an actual `429` from Strava would indicate a bug (headroom logic failed). `BudgetExhausted` is caught in sync services and surfaces as `task.status = "rate_limited"` with a `retry_after` timestamp the frontend can display.
