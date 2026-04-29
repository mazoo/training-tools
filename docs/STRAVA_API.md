# Strava API Integration

## Authentication

Strava uses OAuth 2.0. The flow:

1. Frontend redirects athlete to `https://www.strava.com/oauth/authorize` with:
   - `client_id`
   - `redirect_uri` = `http://localhost:8000/auth/strava/callback` (dev, matches registered Strava app)
   - `response_type=code`
   - `scope=read,activity:read,profile:read_all`  — **minimum required scopes for this app**
2. Strava redirects back with `?code=…`
3. Backend exchanges the code for tokens: `POST https://www.strava.com/oauth/token`
4. Backend stores `access_token`, `refresh_token`, `expires_at` in `athlete_tokens`.
5. Before every API call, check `expires_at`; if `now + 60s > expires_at`, refresh with `refresh_token`.

### Required OAuth scopes

| Scope | Why needed |
|-------|-----------|
| `read` | Read public segments and segment geometry |
| `activity:read` | Read athlete's own activities and segment efforts within them |
| `profile:read_all` | Read athlete profile |

## Rate limits

Strava enforces two windows per application (not per user):

| Window | Limit |
|--------|-------|
| 15 minutes | 200 requests |
| Daily (UTC midnight) | 2 000 requests |

**Since this app starts as single-user, the full budget belongs to that one athlete.**

### Rate-limit implementation

`backend/app/strava/rate_limiter.py` implements a **proactive token bucket** via a module-level singleton `rate_limiter`. The strategy is to raise *before* the HTTP call, not after a 429 response.

Every outgoing Strava request calls `await rate_limiter.acquire()` first. If remaining budget is at or below a headroom threshold, `BudgetExhausted` is raised immediately — no sleep, no waiting:

| Window | Limit | Headroom (raises when remaining is <=) |
|--------|-------|------------------------------|
| 15 min | 200 | 20 |
| Daily  | 2000 | 100 |

After each response, `rate_limiter.sync_from_headers(headers)` overwrites the local counters from `X-RateLimit-Usage` / `X-RateLimit-Limit` (ground truth). State is persisted to `rate_limit_state.json` so budget survives process restarts.

Receiving an actual `429` from Strava indicates the headroom logic failed and should be treated as a bug. `BudgetExhausted` is the normal exhaustion signal.

Gap-first onboarding uses a hard 150-call cap per new athlete: starred-segment pages first, then KOM-time enrichment calls for the highest-value PR-seeded segments. The KOM/QOM page's browser-triggered backfill uses the same 15-minute headroom and a stricter daily threshold of 150 remaining calls before showing or starting the button. Cron backfill also stops once daily remaining calls drop below 150 and spends at most 10 calls per invocation across all athletes.

### Staying safe

- Never fire requests in an unbounded loop without checking the limiter.
- Parallel requests are fine (use `asyncio.gather`) but each must still acquire a token.
- Cache aggressively (see `docs/ARCHITECTURE.md`) to reduce total calls.

## Key endpoints used

### Starred segments (paginated)

```
GET /athlete/segments/starred
  ?page=1&per_page=200
Headers: Authorization: Bearer {access_token}
```

Returns an array of `SummarySegment` objects. Paginate until fewer than `per_page` results are returned.

Important fields:
```json
{
  "id": 123456,
  "name": "Col de la Croix",
  "distance": 4321.0,
  "average_grade": 7.8,
  "start_latlng": [46.123, 7.456],
  "city": "Villars-sur-Ollon",
  "country": "Switzerland",
  "starred": true,
  "starred_date": "2026-04-01T10:00:00Z",
  "athlete_pr_effort": {
    "activity_id": 7419766464,
    "elapsed_time": 431,
    "start_date": "2022-07-05T15:32:49Z",
    "is_kom": false
  }
}
```

`athlete_pr_effort` is the primary first-load seed: it gives the athlete's known PR time/date/activity on starred segments without calling activity detail or segment-effort endpoints. When `athlete_pr_effort.is_kom` is true, the app infers `kom_time_s = pr_time_s` and gap `0` without calling `GET /segments/{id}`.

### Detailed activity — recent sync source of rank and watts data

```
GET /activities/{id}?include_all_efforts=true
Headers: Authorization: Bearer {access_token}
```

> **Always pass `include_all_efforts=true`.** Without it Strava returns only the athlete's PR efforts per segment, silently omitting non-PR segment efforts. This is the single most important parameter for correct `top10_seen` / `podium_seen` tracking.

This is the recent/manual sync data source for KOM/QOM signals. The response includes a `segment_efforts` array. Each element represents one time the athlete passed through that segment during this activity:

```json
{
  "segment_efforts": [
    {
      "id": 9876543,
      "segment": {
        "id": 123456,
        "name": "Col de la Croix"
      },
      "elapsed_time": 280,
      "moving_time": 278,
      "average_watts": 318.5,
      "kom_rank": 3,
      "pr_rank": 1
    }
  ]
}
```

| Field | Notes |
|-------|-------|
| `kom_rank` | Athlete's leaderboard rank **at time of this effort**. `null` if outside top 10 at that moment. This is the source for `top10_seen` / `podium_seen`. |
| `pr_rank` | `1` if this was a PR at time of effort, `null` otherwise. |
| `average_watts` | Average power for this segment effort. `null` if no power meter. Source for `best_avg_watts`. |
| `elapsed_time` | Wall-clock duration in seconds. Source for `best_time_s`. |

> **Important:** `kom_rank` reflects the leaderboard state at the time the effort was recorded. It is not updated retroactively if someone subsequently beats the athlete's time. For our purposes (`top10_seen`, `podium_seen`) this is the correct and only reliable rank signal available via the Strava API — the leaderboard endpoint has been removed.

### Segment efforts — historical starred-segment backfill

```
GET /segment_efforts
  ?segment_id={segment_id}
  &start_date_local={iso_datetime}
  &end_date_local={iso_datetime}
  &per_page=200
Headers: Authorization: Bearer {access_token}
```

Used by daily historical backfill after starred segments are known. This endpoint returns `DetailedSegmentEffort` objects for the authenticated athlete on one segment, including `activity.id`, timing, watts, embedded `segment` metadata, `kom_rank`, and `pr_rank`.

Backfill asks for a broad 365-day window with `per_page=200`. In the normal case this returns all efforts for that starred segment in one call. If Strava returns exactly 200 efforts, the result may be capped, so the service splits the date window and retries each half until each window returns fewer than 200 efforts.

> **Availability:** Strava documents this endpoint as requiring a subscription. If a segment-effort request returns a permanent per-segment error such as 400/403/404, the backfill records it as skipped and continues; transient errors are retried on a later run. Recent/manual sync via detailed activities remains available.

### Segment detail (enrichment only)

```
GET /segments/{id}
Headers: Authorization: Bearer {access_token}
```

Used by gap-first onboarding and later KOM-time backfill, cached **7 days**. Onboarding spends remaining calls from its 150-call budget here after starred segments are synced. Background cron backfill spends at most 10 calls per invocation across all athletes, prioritizing high-value candidates first.

```json
{
  "id": 123456,
  "distance": 4321.0,
  "average_grade": 7.8,
  "maximum_grade": 14.2,
  "start_latlng": [46.123, 7.456],
  "city": "Villars-sur-Ollon",
  "country": "Switzerland",
  "climb_category": 3,
  "xoms": {
    "kom": "4:20",
    "qom": "6:30"
  },
  "athlete_segment_stats": {
    "pr_elapsed_time": 280,
    "pr_date": "2025-06-01",
    "effort_count": 7
  }
}
```

**`xoms.kom` availability:** this field was present in earlier Strava API responses but its continued availability is uncertain given Strava's API restrictions. Treat `kom_time_s` as optional enrichment — the app works without it. If `xoms` is absent or null, store `null` in `segment_enrichment.kom_time_s` and update `kom_time_checked_at` so the segment is not retried until the cache expires. Note: `gap_to_kom_s` is **not stored** — it is computed at query time from imported `best_time_s` when available, otherwise from seeded `pr_time_s`.

Parsing `xoms.kom` to seconds:
```python
def xom_to_seconds(s: str) -> int:
    parts = s.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
```

### Activity list (paginated)

```
GET /athlete/activities
  ?page=1&per_page=200&after={unix_timestamp}
Headers: Authorization: Bearer {access_token}
```

Used during bootstrap and incremental sync. Returns `SummaryActivity` objects (no segment efforts). Use `after` parameter to fetch only activities newer than the last sync timestamp.

### Refresh token

```
POST https://www.strava.com/oauth/token
Body (form): client_id=… client_secret=… grant_type=refresh_token refresh_token=…
```

Response contains new `access_token`, `refresh_token`, `expires_at`.

## Indoor vs outdoor detection

Strava does not expose an `indoor` flag on segments directly. Inference rules (applied in priority order):

1. `segment.start_latlng` is null or `[0, 0]` → indoor
2. `segment.activity_type == "VirtualRide"` → indoor (most reliable signal; set by Strava for Zwift etc.)
3. Segment name matches `(?i)(zwift|virtual|indoor|trainer)` → indoor
4. Default → outdoor

Stored as `is_indoor` on `athlete_segment_profile`. Re-evaluated on every starred-segment sync because `activity_type` is now available from `GET /segments/starred`.

## Useful headers in every response

| Header | Meaning |
|--------|---------|
| `X-RateLimit-Limit` | `200,2000` (15-min, daily) |
| `X-RateLimit-Usage` | `12,45` (used so far in window) |

Always log these at DEBUG level to aid troubleshooting.

## Error responses

| HTTP status | Meaning | Action |
|-------------|---------|--------|
| 401 | Token expired / invalid | Refresh or re-auth |
| 403 | Insufficient scope | Re-auth with correct scopes |
| 404 | Segment not in Strava | Remove from enrichment cache, skip |
| 429 | Rate limit exceeded | Wait, read `X-RateLimit-Usage` |
| 500/503 | Strava server error | Retry with exponential back-off (max 3×) |
