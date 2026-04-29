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

## Authorization model

Authentication is Strava OAuth plus a signed session token. Authorization is local: `roles`, `permissions`, `role_permissions`, and `athlete_roles` define privileged app capabilities without depending on Strava.

Startup seeds an `admin` role and the `backfill_from_ui` and `strava_api_token_visible` permissions, then grants `admin` to the only connected athlete if no role assignments exist. `/api/auth/me` returns the athlete's `roles` and `permissions`. Browser-triggered historical backfill is protected server-side by `backfill_from_ui` and is only exposed in the UI while the Strava budget has safe 15-minute and daily headroom. The profile-page Strava access-token debug view is protected server-side by `strava_api_token_visible`.

## Two-layer data model

**Layer 1 — Effort import (primary, reliable)**

Recent/manual sync imports efforts from `GET /activities/{id}?include_all_efforts=true`. Historical backfill imports starred-segment efforts directly from `GET /segment_efforts`. In both cases, each effort carries `kom_rank` (null if the athlete was outside top 10 at the time), `average_watts`, `elapsed_time`, and `moving_time`. These are stored verbatim in `segment_effort_digest` and then aggregated into `athlete_segment_profile`.

`top10_seen` and `podium_seen` are derived here and are the primary signals for KOM/QOM candidate identification. They do not depend on the leaderboard endpoint (which Strava removed).

**Layer 2 — Segment enrichment (optional)**

Geometry (`start_latlng`, `avg_grade_pct`), city/country, elevation, and `activity_type` come free from `GET /athlete/segments/starred` and are stored in `segment_enrichment` on every sync. Starred segments also provide athlete-specific PR metadata via `athlete_pr_effort`, which seeds first-load candidates before full effort history is imported. `kom_time_s` (from `xoms.kom`) is populated by gap-first onboarding and by backfill via `GET /segments/{id}`. If `athlete_pr_effort.is_kom = true`, `kom_time_s` is inferred from `pr_time_s` without a detail call. `kom_time_checked_at` records attempts even when `xoms.kom` is absent, so missing optional data does not get retried every run.

`gap_to_kom_s` is **not stored** — it is computed at query time from the imported best effort time when available, otherwise from the starred PR time. This avoids a stale derived column and lets home-distance calculation (also query-time) stay consistent.

## Data flow: KOM/QOM candidates

### Bootstrap (first connect)

```
1. Sync starred segments  (GET /athlete/segments/starred, paginated)
   └─ upsert geometry/grade/elevation/activity_type into segment_enrichment
      upsert is_kom/pr_time_s/starred_date into athlete_segment_profile
      infer kom_time_s = pr_time_s for current KOM/QOM holders

2. Spend remaining calls from the 150-call onboarding budget on KOM-time enrichment:
   GET /segments/{id}
   └─ only for PR-seeded starred segments that do not already have known KOM time
   └─ order by "best chance" candidate priority (rank history if known,
      outdoor rides, realistic duration/grade, recent PR/star date, segment id)

3. Set bootstrap_done = true, last_activity_sync_at = now

   No activity fetch on bootstrap — effort history density comes from the backfill.
```

### Incremental refresh ("Refresh data" button)

```
1. Sync starred segments  (GET /athlete/segments/starred)
   └─ same upsert as bootstrap

2. Fetch new activities   (GET /athlete/activities?after=last_activity_sync_at)
   └─ for each activity: GET /activities/{id}?include_all_efforts=true
      └─ extract segment_efforts[]
         → insert into segment_effort_digest
         → recompute athlete_segment_profile (times_ridden, best_time_s,
           best_avg_watts, top10_seen, podium_seen, best_seen_kom_rank)
```

### Historical backfill

```
1. Fetch KOM-time enrichment while the run still has call budget:
   GET /segments/{id}
   └─ parse xoms.kom into segment_enrichment.kom_time_s when present
   └─ update segment_enrichment.kom_time_checked_at even when xoms.kom is absent
   └─ order: high-value candidates first, using the same best-chance priority as onboarding

2. Read pending starred segments from local DB
   └─ skip segments already marked done/skipped in segment_effort_backfill_state
   └─ prioritize already gap-enriched candidates, then remaining starred segments

3. For each pending starred segment:
   GET /segment_efforts?segment_id={id}&start_date_local={now-365d}&end_date_local={now}&per_page=200
   └─ normally returns all efforts for that segment in one call
   └─ if exactly 200 efforts are returned and the run budget is exhausted, leave pending
      so a future run can continue safely

4. Store efforts in segment_effort_digest
   └─ recompute athlete_segment_profile for touched segments
   └─ mark the segment done/skipped so a rate-limited run resumes at the next segment
```

### Query path (hot, no Strava calls)

```
GET /api/kom-qom/candidates
        │
        ├─ 1. Read athlete_segment_profile WHERE is_starred = true
        │      AND (times_ridden > 0 OR pr_time_s IS NOT NULL)
        │
        ├─ 2. JOIN segment_enrichment  (for grade, geometry, xoms)
        │      Compute distance_from_home_km = haversine(start_lat, start_lng, home_lat, home_lng)
        │      Compute gap_to_kom_s from best_time_s or seeded pr_time_s
        │      (null if kom_time_s is unknown)
        │      (both are cheap calculations; neither is stored)
        │
        ├─ 3. Apply filters
        │      effort_time_min / effort_time_max  → COALESCE(best_time_s, pr_time_s)
        │      gradient_min / gradient_max        → avg_grade_pct
        │      surface                            → is_indoor
        │      podium_only                        → podium_seen = true
        │
        └─ 4. Sort: KOMs first, then by distance_from_home ASC (nulls last), return JSON
```

## Caching / sync strategy

| Data | Source | Update trigger | TTL |
|------|--------|----------------|-----|
| Starred segments (geometry, PR, is_kom) | `GET /athlete/segments/starred` | Every sync | — |
| Activity list | `GET /athlete/activities` | Incremental (newest-first, stop at last known) | — |
| Activity details | `GET /activities/{id}` | Once per activity | — |
| Starred segment efforts | `GET /segment_efforts` | Daily historical backfill; one broad call per starred segment in the normal case | — |
| KOM time enrichment | `GET /segments/{id}` | Gap-first onboarding; then daily/UI backfill within call budget | 7 days |

On a full cold-start for an athlete with 100 starred segments:

- 1 call: starred segments
- up to 149 calls: KOM-time enrichment for best-chance PR-seeded candidates
- 0 calls: activity detail fetches during bootstrap

On a typical incremental refresh with 3 new activities since last sync:

- 1 call: starred segments
- 1 call: activity list
- 3 calls: activity details

Historical backfill then proceeds separately. The cron path spends at most 10 Strava calls per 15-minute window across all athletes and rotates athletes round-robin. It first fills missing/stale KOM-time enrichment for high-value candidates, then uses one `GET /segment_efforts` call per starred segment in the normal case. Dense segments that hit the `per_page=200` cap stay pending if the run budget is exhausted.

Backfill normally runs through `POST /api/internal/daily-backfill` with `BACKFILL_SECRET`. Admin users with `backfill_from_ui` can also start their own backfill chunk from the KOM/QOM page, but the button is hidden unless the current Strava budget remains above the 15-minute headroom and the stricter daily backfill threshold.

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

-- Local authorization.
CREATE TABLE roles (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,       -- e.g. admin
    label   TEXT NOT NULL
);

CREATE TABLE permissions (
    id           INTEGER PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,  -- e.g. backfill_from_ui, strava_api_token_visible
    label        TEXT NOT NULL,
    description  TEXT
);

CREATE TABLE role_permissions (
    role_id        INTEGER NOT NULL,
    permission_id  INTEGER NOT NULL,
    PRIMARY KEY (role_id, permission_id)
);

CREATE TABLE athlete_roles (
    athlete_id  INTEGER NOT NULL,
    role_id     INTEGER NOT NULL,
    PRIMARY KEY (athlete_id, role_id)
);

-- Sync progress per athlete
CREATE TABLE athlete_sync_state (
    athlete_id              INTEGER PRIMARY KEY,
    bootstrap_done          BOOLEAN DEFAULT 0,  -- true after the first run_sync completes
    last_activity_sync_at   DATETIME,           -- updated after each run_sync
    last_star_sync_at       DATETIME,           -- updated after each starred-segment fetch
    backfill_cursor_at      DATETIME,           -- oldest timestamp covered once historical backfill completes
    backfill_complete       BOOLEAN DEFAULT 0    -- reset on starred sync to catch newly starred segments
);

-- Per-starred-segment historical backfill progress.
CREATE TABLE segment_effort_backfill_state (
    athlete_id       INTEGER NOT NULL,
    segment_id       INTEGER NOT NULL,
    status           TEXT NOT NULL,      -- pending | done | skipped
    completed_at     DATETIME,
    last_attempt_at  DATETIME,
    last_error       TEXT,
    PRIMARY KEY (athlete_id, segment_id)
);

-- One row per segment effort.
-- Sources: GET /activities/{id} → segment_efforts[], GET /segment_efforts
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
-- Athlete-specific starred-segment fields (is_kom, pr_time_s, starred_date) written on every sync.
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
    is_kom                  BOOLEAN DEFAULT 0,   -- currently holds KOM per Strava
    pr_time_s               INTEGER,             -- Strava authoritative all-time PR
    pr_activity_id          INTEGER,
    pr_date                 DATETIME,
    starred_date            DATETIME,
    updated_at              DATETIME NOT NULL,
    PRIMARY KEY (athlete_id, segment_id)
);

-- Segment metadata shared across athletes.
-- Geometry/grade/elevation populated from GET /segments/starred on every sync (no TTL).
-- kom_time_s is populated by onboarding/backfill GET /segments/{id}, or inferred from pr_time_s for current KOMs.
-- gap_to_kom_s is NOT stored — computed at query time from best_time_s or pr_time_s.
CREATE TABLE segment_enrichment (
    segment_id              INTEGER PRIMARY KEY,
    segment_name            TEXT,
    distance_m              REAL,
    avg_grade_pct           REAL,
    max_grade_pct           REAL,
    start_lat               REAL,
    start_lng               REAL,
    end_lat                 REAL,
    end_lng                 REAL,
    city                    TEXT,
    country                 TEXT,
    state                   TEXT,
    climb_category          INTEGER,
    elevation_high          REAL,
    elevation_low           REAL,
    activity_type           TEXT,           -- "Ride" / "Run" / "VirtualRide"; primary indoor signal
    hazardous               BOOLEAN,
    kom_time_s              INTEGER,        -- parsed from xoms.kom; null if unavailable
    kom_time_checked_at     DATETIME,       -- last GET /segments/{id} attempt
    cached_at               DATETIME NOT NULL
);
```

## Rate-limit strategy

The rate limiter (`strava/rate_limiter.py`) is **proactive**, not reactive. It maintains an in-process token-bucket and raises `BudgetExhausted` *before* making an HTTP call when headroom thresholds are breached:

- 15-min window: raises when `remaining <= 20` (out of 200)
- Daily window: raises when `remaining <= 100` (out of 2000)

Gap-first onboarding has a hard 150-call cap per new athlete. UI-triggered backfill uses the same 15-minute headroom and a stricter daily visibility/start threshold of 150 remaining calls. Cron backfill also keeps that daily buffer and spends at most 10 calls per invocation across all athletes.

After each Strava response the limiter syncs its counters from `X-RateLimit-Usage` / `X-RateLimit-Limit` headers (ground truth) and persists state to `rate_limit_state.json` so budget survives process restarts.

Receiving an actual `429` from Strava would indicate a bug (headroom logic failed). `BudgetExhausted` is caught in sync services and surfaces as `task.status = "rate_limited"` with a `retry_after` timestamp the frontend can display.
