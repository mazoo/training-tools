import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.database import Base, engine
from app.routers import auth, kom_qom, profile

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add columns introduced after initial schema (safe to re-run)
        for col, col_type in (("home_address", "TEXT"), ("home_lat", "REAL"), ("home_lng", "REAL")):
            try:
                await conn.execute(
                    text(f"ALTER TABLE athlete_profile ADD COLUMN {col} {col_type}")
                )
            except Exception:
                pass
    yield


app = FastAPI(title="Training Tools API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4321"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(kom_qom.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
