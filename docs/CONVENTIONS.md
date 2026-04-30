# Conventions — Patterns for Adding to This Codebase

## Adding a new feature (full-stack)

1. **Schema** (`backend/app/schemas/<feature>.py`) — Pydantic models for request/response. Keep one file per feature.
2. **Service** (`backend/app/services/<feature>.py`) — all business logic. No `httpx`/`aiohttp` here; call `StravaClient` for Strava data.
3. **Router** (`backend/app/routers/<feature>.py`) — thin FastAPI endpoints. Inject `db: AsyncSession = Depends(get_db)` and `athlete_id: int = Depends(get_current_athlete_id)`.
4. **Register router** in `main.py`: `app.include_router(<feature>.router)`.
5. **Frontend page** (`frontend/src/pages/<feature>.astro`) — one page per tool. Use inline `<script>` for interactivity; no separate TS files unless the logic is substantial.

## Authentication pattern

Every protected endpoint gets the athlete's ID via the `get_current_athlete_id` dependency from `services/auth.py`:

```python
from app.services.auth import get_current_athlete_id

@router.get("/api/my-feature")
async def my_endpoint(
    athlete_id: int = Depends(get_current_athlete_id),
    db: AsyncSession = Depends(get_db),
):
    ...
```

To make Strava API calls, get a fresh access token:

```python
from app.services.auth import get_valid_access_token

access_token = await get_valid_access_token(athlete_id, db)
client = StravaClient(access_token)
```

`get_valid_access_token` transparently refreshes the token if it expires within 60 s.

For the first SQLite-backed production deployment, `ALLOWED_ATHLETE_IDS` should be set to a comma-separated list of approved Strava athlete IDs. The Strava OAuth callback rejects non-allowlisted athletes before token storage.

## Permission pattern

Roles and permissions live in `models/athlete.py`, with helpers in `services/permissions.py`. Startup seeds the built-in `admin` role, the `backfill_from_ui` and `strava_api_token_visible` permissions, and grants `admin` to the only connected athlete when no role assignments exist yet.

Use `athlete_has_permission(db, athlete_id, BACKFILL_FROM_UI)` when an endpoint needs to report availability without failing. Use `Depends(require_permission(BACKFILL_FROM_UI))` when the endpoint itself must be blocked with `403 Missing permission`.

## Background task pattern

Long operations (sync, enrichment) run as FastAPI `BackgroundTasks` and expose a status endpoint for polling.

```python
from app.tasks import create_task, get_task

@router.post("/api/my-feature/start")
async def start(background_tasks: BackgroundTasks, ...):
    task = create_task()
    background_tasks.add_task(_do_work, task)
    return {"task_id": task.task_id}

@router.get("/api/my-feature/status/{task_id}")
async def status(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    return task

async def _do_work(task: TaskStatus):
    try:
        # update task.activities_processed etc. as you go
        task.status = "done"
    except BudgetExhausted as e:
        task.status = "rate_limited"
        task.retry_after = datetime.now(timezone.utc) + timedelta(seconds=...)
    except Exception as e:
        task.status = "error"
        task.error = str(e)
```

Tasks are in-memory; they are lost on server restart. That is intentional — they are ephemeral UI state.

## Adding a new DB column to an existing table

Do **not** use Alembic. Add an entry to the `migrations` list in `main.py` lifespan:

```python
migrations = [
    ...
    ("my_table", "new_column", "TEXT"),   # add here
]
```

Each entry runs `ALTER TABLE <table> ADD COLUMN <col> <type>`. The `try/except` wrapper makes it idempotent (safe to re-run). Use SQLite type names: `TEXT`, `INTEGER`, `REAL`, `BOOLEAN DEFAULT 0`, `DATETIME`.

For new tables, add the ORM model and `Base.metadata.create_all` handles it automatically at startup.

## Adding a new DB model

1. Create or extend a file in `backend/app/models/`.
2. Import it anywhere before `Base.metadata.create_all` runs — importing in `main.py` at the top is the simplest approach.
3. Document the new model in `docs/DATA_MODELS.md`.

## Strava API calls

Always go through `StravaClient`. Never import `httpx` directly in services or routers.

```python
from app.strava.client import StravaClient

client = StravaClient(access_token)
activities = await client.get_activities(after=since_ts, per_page=100)
```

The rate limiter (`StravaRateLimiter`) is a module-level singleton in `strava/rate_limiter.py`. `StravaClient` calls `rate_limiter.acquire()` before every request and `rate_limiter.sync_from_headers()` after. You never need to touch the rate limiter directly.

Handle `BudgetExhausted` from `strava.rate_limiter` in sync services and surface it as `task.status = "rate_limited"` or `HTTPException(429)`.

## Internal / cron endpoints

Endpoints not meant for the browser go in `routers/internal.py` under `/api/internal/`. Protect them with the `BACKFILL_SECRET` pattern from that file (Bearer token check, not the session auth system).

## Utility functions

Before writing new helpers, check `utils.py`:
- `haversine_km(lat1, lon1, lat2, lon2)` — distance in km
- `xom_to_seconds(s)` — parse `"MM:SS"` or `"H:MM:SS"` KOM time string
- `seconds_to_display(s)` — format seconds back to display string
- `is_segment_indoor(segment_dict)` — detect virtual/indoor segments

## Claude Code local overrides

`.claude/settings.local.json` (gitignored) lets each developer override the shared `.claude/settings.json` without affecting others. Useful for:
- Disabling the Stop hook temporarily (`"hooks": {}`)
- Personal permission rules or allowed tools
- A different model preference for this project

Settings load in order: `settings.json` → `settings.local.json`, with local taking precedence.

## Frontend conventions

- Auth state is stored in `localStorage` as a session token (key: `tt_token`). `Layout.astro` manages it.
- API calls use plain `fetch("/api/...")` with `Authorization: Bearer <token>` header. No SDK.
- Show loading state immediately; update UI on poll response. The status endpoint returns `task.status` ∈ `{running, done, error, rate_limited}` plus current Strava budget counters.
- Tailwind utility classes only; no custom CSS unless unavoidable.
