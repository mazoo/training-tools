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

Every outgoing Strava request calls `await rate_limiter.acquire()` first. If remaining budget is below a headroom threshold, `BudgetExhausted` is raised immediately — no sleep, no waiting:

| Window | Limit | Headroom (raises when below) |
|--------|-------|------------------------------|
| 15 min | 200 | 20 |
| Daily  | 2000 | 100 |

After each response, `rate_limiter.sync_from_headers(headers)` overwrites the local counters from `X-RateLimit-Usage` / `X-RateLimit-Limit` (ground truth). State is persisted to `rate_limit_state.json` so budget survives process restarts.

Receiving an actual `429` from Strava indicates the headroom logic failed and should be treated as a bug. `BudgetExhausted` is the normal exhaustion signal.

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
  "starred": true
}
```

### Detailed activity — primary source of rank and watts data

```
GET /activities/{id}?include_all_efforts=true
Headers: Authorization: Bearer {access_token}
```

> **Always pass `include_all_efforts=true`.** Without it Strava returns only the athlete's PR efforts per segment, silently omitting non-PR segment efforts. This is the single most important parameter for correct `top10_seen` / `podium_seen` tracking.

This is the **primary data source** for KOM/QOM signals. The response includes a `segment_efforts` array. Each element represents one time the athlete passed through that segment during this activity:

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

### Segment detail (enrichment only)

```
GET /segments/{id}
Headers: Authorization: Bearer {access_token}
```

Used only for segment geometry and the optional KOM time. Called only for starred segments, cached **7 days**.

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

**`xoms.kom` availability:** this field was present in earlier Strava API responses but its continued availability is uncertain given Strava's API restrictions. Treat `kom_time_s` as optional enrichment — the app works without it. If `xoms` is absent or null, store `null` in `segment_enrichment.kom_time_s`. Note: `gap_to_kom_s` is **not stored** — it is computed at query time as `best_time_s - kom_time_s`.

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

1. `segment.start_latlng == [0.0, 0.0]` or null → indoor
2. Segment name matches `(?i)(zwift|virtual|indoor|trainer)` → indoor
3. Default → outdoor

Stored as `is_indoor` on `athlete_segment_profile`, computed once on first profile build.

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
