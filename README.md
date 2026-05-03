# Training Tools

## What this project is

A personal web application suite for cycling/running training analysis, built on top of the Strava API. The user (single athlete) wants data-driven tooling to support performance decisions.

## Tech stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Backend | Python 3.12 + FastAPI | Async-native, great for I/O-bound Strava API work |
| DB | SQLite (dev / private first production) → PostgreSQL before public users | Simple local dev, low-cost launch path, clear migration before open signup |
| Frontend | Astro 5 + TailwindCSS | MPA with islands; great for content-heavy tool pages |
| Auth | Strava OAuth 2.0 + session token (itsdangerous) + DB roles/permissions | Strava token stored in DB; session signed with `SECRET_KEY`; privileged UI actions are permission-gated |
| Rate limiting | Singleton in-process token-bucket, persisted across restarts | See `docs/STRAVA_API.md` and `backend/app/strava/rate_limiter.py` |

## Repository layout

```
training-tools/
├── README.md                  ← you are here
├── CLAUDE.md                  ← Claude Code entry point (@imports README + docs/)
├── .claude/
│   ├── settings.json          ← shared project config: Stop hook, permissions (tracked)
│   └── settings.local.json    ← personal overrides per developer (gitignored)
├── docs/
│   ├── ARCHITECTURE.md        ← system design & data flow
│   ├── DEPLOYMENT.md          ← low-cost AWS Lightsail production setup
│   ├── STRAVA_API.md          ← API integration contract & rate limits
│   ├── CONVENTIONS.md         ← coding patterns: how to add features
│   ├── DATA_MODELS.md         ← DB schema reference
│   └── features/
│       └── KOM_QOM_CANDIDATES.md  ← feature spec: KOM/QOM tool
├── backend/
│   ├── app/
│   │   ├── main.py            ← FastAPI app entry point + inline schema migrations
│   │   ├── config.py          ← pydantic-settings, reads .env
│   │   ├── database.py        ← SQLAlchemy async engine + session dependency
│   │   ├── tasks.py           ← in-memory background task tracker (UUID-keyed)
│   │   ├── utils.py           ← haversine, time formatting, indoor detection
│   │   ├── models/
│   │   │   ├── athlete.py     ← AthleteToken, AthleteProfile, AthleteSyncState, roles/permissions
│   │   │   └── segment.py     ← SegmentEffortDigest, profiles, enrichment, segment backfill state
│   │   ├── schemas/
│   │   │   └── kom_qom.py     ← Pydantic response schemas for KOM/QOM feature
│   │   ├── routers/
│   │   │   ├── auth.py        ← Strava OAuth login/callback, /api/auth/me
│   │   │   ├── profile.py     ← home address update, token debug view, disconnect, account deletion
│   │   │   ├── kom_qom.py     ← KOM/QOM candidates list, refresh, UI backfill, status polling
│   │   │   └── internal.py    ← POST /api/internal/daily-backfill (cron endpoint)
│   │   ├── services/
│   │   │   ├── auth.py        ← session token create/decode, access token validation + refresh
│   │   │   ├── sync.py        ← Strava sync orchestration (starred segments, activities, segment-effort backfill)
│   │   │   ├── permissions.py ← role/permission seeding and checks
│   │   │   └── kom_qom.py     ← KOM/QOM candidate filtering, sex-aware gap computation
│   │   └── strava/
│   │       ├── client.py      ← StravaClient: OAuth exchange, athlete/activity/segment-effort API
│   │       └── rate_limiter.py← StravaRateLimiter: token-bucket, header sync, disk persistence
│   ├── pyproject.toml
│   └── .env.example
├── deploy/
│   ├── Caddyfile              ← production reverse proxy template
│   ├── env.production.example ← production environment template
│   ├── scripts/               ← operational scripts used by systemd
│   └── systemd/               ← API, backfill, and SQLite backup units
└── frontend/
    ├── public/               ← static assets served from site root (favicon, logo assets)
    ├── src/
    │   ├── pages/             ← .astro files, one per tool page
    │   ├── layouts/           ← shared Layout.astro (nav, footer + auth state)
    │   └── styles/            ← global.css (Tailwind base)
    ├── astro.config.mjs
    └── package.json
```

## Documentation

| File | Description |
|------|-------------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, data flow, and two-layer data model |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | AWS Lightsail + GitHub Actions production deployment |
| [docs/STRAVA_API.md](docs/STRAVA_API.md) | Strava API integration contract, rate limits, and key endpoints |
| [docs/CONVENTIONS.md](docs/CONVENTIONS.md) | Coding patterns: how to add features, background tasks, migrations |
| [docs/DATA_MODELS.md](docs/DATA_MODELS.md) | Database schema reference with ER diagram |
| [docs/features/KOM_QOM_CANDIDATES.md](docs/features/KOM_QOM_CANDIDATES.md) | Feature spec: KOM/QOM candidates tool |

## Running locally

```bash
# Backend
cd backend
cp .env.example .env          # fill in all required vars (see section below)
uv sync
uv run uvicorn app.main:app --reload --port 8000
# Schema is created automatically on first start via SQLAlchemy create_all.
# Incremental column additions are handled by inline ALTER TABLE migrations in main.py lifespan.

# Frontend
cd frontend
npm install
npm run dev                   # runs on :4321 (Astro default), proxies /api → :8000
```

## Key conventions

- **No direct Strava API calls outside `backend/app/strava/`**. All callers go through `StravaClient` so rate limiting is always enforced.
- **Cache Strava responses in DB.** Segment details (including KOM/QOM times from `xoms`) are cached in `SegmentEnrichment` with a 7-day TTL. Never re-fetch if fresh data is available.
- **Home location** defaults to env vars `HOME_LAT` / `HOME_LNG`. Athletes can override it per-account via `PUT /api/profile/home` (Nominatim geocoding stores result in `AthleteProfile`). Distance is Haversine via `utils.haversine_km`.
- **Privileged UI actions are permission-gated.** Startup seeds the `admin` role plus the `backfill_from_ui` and `strava_api_token_visible` permissions; the first/single connected athlete is granted `admin`.
- **First production is private-gated.** Set `ALLOWED_ATHLETE_IDS` in production so only approved Strava athlete IDs can complete OAuth while SQLite is the production database.
- **Feature routers** live in `backend/app/routers/`. Each feature is one file. Keep routers thin — business logic belongs in `services/`.
- **Background tasks** use `tasks.py` for status tracking. Create with `create_task()`, update in-place, poll via a status endpoint. Tasks are in-memory only (lost on restart).
- **Schema migrations**: there is no Alembic. New tables are created by `Base.metadata.create_all` at startup. New columns on existing tables go into the `migrations` list in `main.py` lifespan as `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`-style entries (wrapped in try/except for idempotency).
- **Error handling**: propagate Strava API errors as `HTTPException` with a `strava_error` detail key so the frontend can show a helpful message.
- **No secrets in code**. Everything sensitive goes in `.env` (gitignored).

See `docs/CONVENTIONS.md` for step-by-step patterns (new feature, new background task, new model column).

## Environment variables (`.env`)

```
STRAVA_CLIENT_ID=
STRAVA_CLIENT_SECRET=
STRAVA_REDIRECT_URI=http://localhost:8000/auth/strava/callback
FRONTEND_URL=http://localhost:4321
DATABASE_URL=sqlite+aiosqlite:///./training_tools.db
RATE_LIMIT_STATE_PATH=rate_limit_state.json
# Generate both with: python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=
BACKFILL_SECRET=
ALLOWED_ATHLETE_IDS=
HOME_LAT=
HOME_LNG=
```

## Production deployment

The first production target is a single AWS Lightsail Linux instance with Caddy serving `frontend/dist` and proxying `/api`, `/auth`, and `/health` to FastAPI on `127.0.0.1:8000`. GitHub Actions builds, tests, uploads, activates the release, restarts systemd services, and smoke-tests the public health endpoint.

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for server bootstrap, required GitHub secrets, Strava redirect configuration, SQLite backups, and the Postgres-before-public-users path.

## Testing

```bash
cd backend
uv run pytest -x
```

Frontend:
```bash
cd frontend
npm test
```
