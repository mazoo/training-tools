# Feature: KOM/QOM Candidates

## Goal

Show the athlete a list of their starred Strava segments where they have historically appeared in the top 10 or on the podium, so they can identify realistic KOM/QOM targets. The primary signal — `top10_seen` / `podium_seen` — is derived from `kom_rank` on individual segment efforts, imported from detailed activities during recent syncs and from `GET /segment_efforts` during historical backfill. This approach does not depend on the leaderboard endpoint (removed by Strava).

## User stories

- As an athlete, I want to see which of my starred segments I have ever been in the top 10 or on the podium for, so I can prioritise realistic KOM/QOM targets.
- As an athlete, I want to filter by effort time, gradient, surface, activity type (rides vs runs), and whether I already hold the KOM, so I can match segments to a specific workout goal.
- As an athlete, I want to see how many times I've ridden a segment and what average watts I've put out, so I can gauge my current form.
- As an athlete, I want KOM segments surfaced first, then the rest sorted by distance from home, so I can see what I already hold and plan local efforts next.
- As an admin, I want to start the historical backfill from the page when Strava budget is safe, so I can fill missing segment-effort history without using the cron endpoint manually.

## API contract

### `GET /api/kom-qom/candidates`

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `effort_time_min` | integer (seconds) | — | Minimum best effort time |
| `effort_time_max` | integer (seconds) | — | Maximum best effort time |
| `gradient_min` | float (%) | — | Minimum average gradient |
| `gradient_max` | float (%) | — | Maximum average gradient |
| `surface` | `outdoor` \| `indoor` \| `all` | `all` | Filter by environment |
| `podium_only` | boolean | `false` | If true, return only segments where `podium_seen = true` (backend supported; not currently exposed in the UI — to be re-added once data is verified) |

#### Response `200 OK`

```json
{
  "fetched_at": "2025-07-14T10:00:00Z",
  "total": 12,
  "candidates": [
    {
      "segment_id": 123456,
      "segment_name": "Col de la Croix",
      "top10_seen": true,
      "podium_seen": true,
      "best_seen_kom_rank": 2,
      "last_seen_kom_rank": 3,
      "is_kom": false,
      "best_time_s": 847,
      "best_time_display": "14:07",
      "latest_time_s": 901,
      "latest_time_display": "15:01",
      "pr_time_s": 840,
      "pr_time_display": "14:00",
      "pr_date": "2024-08-12T09:15:00Z",
      "times_ridden": 7,
      "best_avg_watts": 328.5,
      "latest_avg_watts": 310.2,
      "last_ridden_at": "2025-06-01T09:30:00Z",
      "starred_date": "2023-08-06T20:42:09Z",
      "kom_time_s": 820,
      "kom_time_display": "13:40",
      "gap_to_kom_s": 27,
      "gap_to_kom_display": "0:27",
      "gap_to_kom_pct": 3.3,
      "average_grade": 7.8,
      "distance_m": 4321,
      "elevation_high": 1775.7,
      "elevation_low": 728.4,
      "distance_from_home_km": 3.2,
      "is_indoor": false,
      "activity_type": "Ride",
      "hazardous": false,
      "city": "Villars-sur-Ollon",
      "state": "Vaud",
      "country": "Switzerland",
      "climb_category": 3,
      "segment_url": "https://www.strava.com/segments/123456"
    }
  ]
}
```

Notes:
- `kom_time_s`, `gap_to_kom_s`, `gap_to_kom_pct`, and their display variants may be `null` if `xoms` enrichment is unavailable.
- `gap_to_kom_pct` is `(best_time_s - kom_time_s) / kom_time_s * 100`, expressing how far off KOM the athlete is as a percentage.
- `is_kom` reflects Strava's `athlete_pr_effort.is_kom` from the starred segment response — true if the athlete currently holds the KOM.
- `pr_time_s` is Strava's authoritative all-time PR for the athlete; may differ from `best_time_s` if history predates our sync window.
- `pr_time_s`, `pr_date`, `starred_date` may be `null` for segments starred before the first sync or with no recorded PR.
- `best_avg_watts` / `latest_avg_watts` may be `null` for athletes without a power meter.
- `elevation_high`, `elevation_low`, `state`, `activity_type`, `hazardous` may be `null` for non-starred segments without enrichment.
- `distance_from_home_km` may be `null` if `HOME_LAT`/`HOME_LNG` are not configured or `start_latlng` is absent.
- `last_ridden_at` is an ISO 8601 datetime string (not date-only).

#### Response `429 Too Many Requests`

```json
{
  "detail": { "strava_error": "Rate limit reached", "retry_after_s": 312 }
}
```

### `POST /api/kom-qom/refresh`

Triggers an activity sync (starred segments + recent activities).
Returns `202 Accepted` immediately; runs in a background task.

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `full` | boolean | `false` | If true, re-sync all activities (not just since last sync) |

```json
{ "task_id": "abc123", "message": "Refresh started" }
```

### `GET /api/kom-qom/refresh/{task_id}`

Poll refresh progress.

```json
{
  "status": "running",
  "activities_processed": 18,
  "activities_total": 25,
  "strava_calls_made": 20,
  "strava_budget_remaining_15min": 180,
  "strava_budget_remaining_daily": 1800,
  "error": null,
  "retry_after": null
}
```

`status` ∈ `{ running, done, error, rate_limited }`. When `status` is `rate_limited`, `retry_after` is an ISO 8601 datetime indicating when to resume.

### `GET /api/kom-qom/backfill/availability`

Returns whether the current athlete can see/start the UI backfill button. Requires a valid session; does not fail just because the permission is missing.

```json
{
  "has_permission": true,
  "available": true,
  "reason": null,
  "strava_budget_remaining_15min": 180,
  "strava_budget_remaining_daily": 1800,
  "retry_after_seconds": null
}
```

`available` is true only when the athlete has `backfill_from_ui`, the 15-minute budget is above the standard headroom, and daily budget is at least the backfill threshold (150 remaining calls). `reason` is one of `missing_permission`, `rate_limited_15min`, or `rate_limited_daily` when unavailable.

### `POST /api/kom-qom/backfill`

Starts this athlete's historical starred-segment effort backfill in a background task. Requires `backfill_from_ui`; returns `403` without it and `429` when the current budget is unsafe.

```json
{ "task_id": "abc123", "message": "Backfill started" }
```

### `GET /api/kom-qom/backfill/{task_id}`

Polls UI backfill progress using the same status shape as refresh. `activities_processed` / `activities_total` represent starred segments for this task.

## Frontend

### Page: `src/pages/kom-qom.astro`

Single `.astro` file with an inline `<script>` block for interactivity. No separate component files — filters and card rendering are handled in the page script.

Layout:
```
┌─────────────────────────────────────────────────────────────┐
│  KOM / QOM Candidates                        [Refresh data] │
├──────────────────────┬──────────────────────────────────────┤
│  FILTERS             │  [ Search segment name…            ] │
│                      ├──────────────────────────────────────┤
│  KOM/QOM included ●  │  SEGMENT LIST (KOMs first, then dist)│
│  Rides only       ●  │                                      │
│                      │  Col de la Croix              3.2km │
│  Effort time         │      Best 14:07 · Last 15:01         │
│  [   ] – [   ] min   │      Rank: #2 seen · 328W · 7×      │
│                      │      Gap to KOM: 0:27 ·  +7.8%      │
│  Gradient            │                                      │
│  [   ] – [   ] %     │  Kleine Scheidegg             5.1km │
│                      │      ...                            │
│  Surface             │                                      │
│  ○ All  ○ Out  ○ In  │                                      │
│                      │                                      │
│  [Run daily backfill]│  visible only with permission +      │
│  [Refresh data]      │  safe Strava budget                  │
└──────────────────────┴──────────────────────────────────────┘
```

The search box and sort dropdown are rendered side-by-side above the candidate list (not in the sidebar). Filtering and sorting are client-side — no extra API call on change.

**Sort options (dropdown, top-right of list):**
- **KOM / Podium / Top-10 + distance** (default): groups by rank tier (KOM → Podium → Top-10), then sorts by `distance_from_home_km` ASC within each tier (nulls last).
- **Distance from home**: flat sort by `distance_from_home_km` ASC regardless of rank (nulls last).

Each segment card shows:
- Rank badge: KOM crown if `is_kom` (currently holds KOM), podium if `podium_seen`, top-10 otherwise
- Segment name + Strava link
- Distance from home (km)
- Best time vs latest time; Strava PR (`pr_time_s`) if available; gap to KOM and `gap_to_kom_pct` (if available)
- Best watts / latest watts (if available)
- Times ridden + last ridden date
- Average grade + total distance + elevation gain (`elevation_high - elevation_low`)
- City / state / country; `hazardous` warning if flagged

Filters are applied client-side — all candidates are fetched once on page load. No extra API call on filter change. The daily backfill button is hidden unless `/api/kom-qom/backfill/availability` reports `available = true`.

## Business logic

### Candidate inclusion rule

A segment is a candidate if, in `athlete_segment_profile`:
1. `is_starred = true`
2. `times_ridden > 0`

`top10_seen` and `podium_seen` are **not** default filters — they are surfaced on each card so the athlete can see their rank history. The `podium_only` filter optionally restricts to `podium_seen = true`.

### Building `top10_seen` / `podium_seen`

When processing segment efforts from either a detailed activity or direct `GET /segment_efforts` response:

```python
for effort in efforts:
    rank = effort.get("kom_rank")       # int 1–10 or None
    upsert_effort_digest(effort)
    if rank is not None:
        profile.top10_seen = True
        if rank <= 3:
            profile.podium_seen = True
        profile.best_seen_kom_rank = min(profile.best_seen_kom_rank or 99, rank)
        profile.last_seen_kom_rank = rank
```

### Aggregating `times_ridden` and watts

```python
profile.times_ridden = COUNT(effort_id) WHERE segment_id = x AND athlete_id = y
profile.best_time_s  = MIN(elapsed_s)   WHERE segment_id = x AND athlete_id = y
profile.best_avg_watts = MAX(avg_watts) WHERE segment_id = x AND athlete_id = y AND avg_watts IS NOT NULL
```

Latest values come from the effort with the most recent `effort_date`.

### Gap to KOM (enrichment, may be null)

```python
# Only if xoms.kom is present in GET /segments/{id} response
kom_time_s = xom_to_seconds(segment["xoms"]["kom"])
gap_to_kom_s = profile.best_time_s - kom_time_s  # 0 if athlete holds KOM
```

If `xoms` is absent, `segment_enrichment.kom_time_s` is stored as `null` and the frontend omits the gap row on the card.

### Distance from home (Haversine)

```python
from math import radians, sin, cos, sqrt, atan2

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))
```

Computed at query time using `start_lat`/`start_lng` from `segment_enrichment` and `HOME_LAT`/`HOME_LNG` from config. Not stored — home location can change without requiring a DB update pass.

### Indoor detection (priority order)

1. `segment.start_latlng` is null or `[0, 0]` → indoor
2. `segment.activity_type == "VirtualRide"` → indoor (most reliable; set by Strava for Zwift etc.)
3. Segment name matches `(?i)(zwift|virtual|indoor|trainer)` → indoor
4. Default → outdoor

## Refresh strategy

1. On first connect (`bootstrap_done = false`), `run_sync` syncs starred segments only (1 Strava call). Effort history is populated by the backfill via `GET /segment_efforts` — no activity fetch on bootstrap.
2. Subsequent "Refresh data" clicks fetch starred segments + activities newer than `last_activity_sync_at`, so today's rides appear immediately without waiting for the next backfill.
3. The frontend polls `/api/kom-qom/refresh/{task_id}` every 3 s and shows a progress bar.
4. Historical data (365-day lookback) comes from the daily backfill cron or the permissioned UI backfill button. It processes starred segments directly with broad `GET /segment_efforts?per_page=200` windows, marking each segment done/skipped so rate-limited runs resume at the next pending segment.

## Edge cases

| Case | Handling |
|------|---------|
| `kom_rank` null on all efforts for a segment | `top10_seen = false`; segment can still appear if ridden, without top-10/podium rank history |
| Athlete has efforts but no power meter | `best_avg_watts = null`; card omits watts row |
| `kom_time_s` null (no enrichment source) | gap row omitted from card |
| Segment deleted from Strava | 404 from `GET /segment_efforts` → skipped, retain profile from effort history |
| `start_latlng` null | `distance_from_home_km = null`; sort these last |
| `HOME_LAT`/`HOME_LNG` not set | `distance_from_home_km = null` for all; disable distance sort, warn in UI |
| Activity deleted on Strava | Remove corresponding `segment_effort_digest` rows; recompute affected profiles |
