# Training Tools — Project Guide for Claude Agents

## What this project is

A personal web application suite for cycling/running training analysis, built on top of the Strava API. The user (single athlete) wants data-driven tooling to support performance decisions.

## Tech stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Backend | Python 3.12 + FastAPI | Async-native, great for I/O-bound Strava API work |
| DB | SQLite (dev) → PostgreSQL (prod) via SQLAlchemy 2.x | Simple local dev, easy migration path |
| Frontend | Astro 5 + TailwindCSS | MPA with islands; great for content-heavy tool pages |
| Auth | Strava OAuth 2.0 (PKCE, token stored in DB) | Strava mandates OAuth; single-user for now |
| Rate limiting | In-process token-bucket per Strava access token | See `docs/STRAVA_API.md` |

## Repository layout

```
training-tools/
├── CLAUDE.md                  ← you are here
├── docs/
│   ├── ARCHITECTURE.md        ← system design & data flow
│   ├── STRAVA_API.md          ← API integration contract & rate limits
│   └── features/
│       └── KOM_QOM_CANDIDATES.md  ← feature spec: KOM/QOM tool
├── backend/
│   ├── app/
│   │   ├── main.py            ← FastAPI app entry point
│   │   ├── config.py          ← pydantic-settings, reads .env
│   │   ├── database.py        ← SQLAlchemy engine + session
│   │   ├── models/            ← ORM models
│   │   ├── schemas/           ← Pydantic request/response schemas
│   │   ├── routers/           ← FastAPI routers, one per feature
│   │   ├── services/          ← Business logic (no HTTP directly)
│   │   └── strava/            ← Strava API client + rate limiter
│   ├── tests/
│   ├── alembic/               ← DB migrations
│   ├── pyproject.toml
│   └── .env.example
└── frontend/
    ├── src/
    │   ├── components/        ← Astro + optional island components
    │   ├── pages/             ← .astro files, one per tool page
    │   ├── layouts/           ← shared Layout.astro
    │   └── api/               ← typed fetch wrappers (TS)
    ├── astro.config.mjs
    └── package.json
```

## Running locally

```bash
# Backend
cd backend
cp .env.example .env          # fill in STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, HOME_LAT, HOME_LNG
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000

# Frontend
cd frontend
npm install
npm run dev                   # runs on :4321 (Astro default), proxies /api → :8000
```

## Key conventions

- **No direct Strava API calls outside `backend/app/strava/`**. All callers go through the client there so rate limiting is always enforced.
- **Cache Strava responses in DB.** Segment details (including KOM time from `xoms`) are cached with a TTL (see `docs/STRAVA_API.md`). Never re-fetch if fresh data is available.
- **Home location** is set via env vars `HOME_LAT` / `HOME_LNG` (decimal degrees). Distance is Haversine from that point to segment start latitude/longitude.
- **Feature routers** live in `backend/app/routers/`. Each feature is one file. Keep routers thin — business logic belongs in `services/`.
- **Error handling**: propagate Strava API errors as `HTTPException` with a `strava_error` detail key so the frontend can show a helpful message.
- **No secrets in code**. Everything sensitive goes in `.env` (gitignored).

## Environment variables (`.env`)

```
STRAVA_CLIENT_ID=
STRAVA_CLIENT_SECRET=
STRAVA_REDIRECT_URI=http://localhost:8000/auth/callback
DATABASE_URL=sqlite+aiosqlite:///./training_tools.db
HOME_LAT=
HOME_LNG=
```

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
