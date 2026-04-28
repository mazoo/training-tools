import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.database import AsyncSessionLocal, Base, engine
from app.config import settings
from app.routers import auth, internal, kom_qom, profile
from app.services.permissions import ensure_default_authorization

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add columns introduced after initial schema (safe to re-run).
        migrations = [
            ("athlete_profile", "home_address", "TEXT"),
            ("athlete_profile", "home_lat", "REAL"),
            ("athlete_profile", "home_lng", "REAL"),
            ("athlete_sync_state", "backfill_cursor_at", "DATETIME"),
            ("athlete_sync_state", "backfill_complete", "BOOLEAN DEFAULT 0"),
            # segment_enrichment: fields from GET /segments/starred (not just IDs)
            ("segment_enrichment", "end_lat", "REAL"),
            ("segment_enrichment", "end_lng", "REAL"),
            ("segment_enrichment", "state", "TEXT"),
            ("segment_enrichment", "elevation_high", "REAL"),
            ("segment_enrichment", "elevation_low", "REAL"),
            ("segment_enrichment", "activity_type", "TEXT"),
            ("segment_enrichment", "hazardous", "BOOLEAN"),
            # athlete_segment_profile: athlete-specific data from starred response
            ("athlete_segment_profile", "is_kom", "BOOLEAN DEFAULT 0"),
            ("athlete_segment_profile", "pr_time_s", "INTEGER"),
            ("athlete_segment_profile", "pr_activity_id", "INTEGER"),
            ("athlete_segment_profile", "pr_date", "DATETIME"),
            ("athlete_segment_profile", "starred_date", "DATETIME"),
        ]
        for table, col, col_type in migrations:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
            except Exception:
                pass
    async with AsyncSessionLocal() as session:
        await ensure_default_authorization(session)
    yield


app = FastAPI(title="Training Tools API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(kom_qom.router)
app.include_router(internal.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
