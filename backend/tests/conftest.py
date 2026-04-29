import os


os.environ.setdefault("STRAVA_CLIENT_ID", "test-client")
os.environ.setdefault("STRAVA_CLIENT_SECRET", "test-secret")
os.environ.setdefault("STRAVA_REDIRECT_URI", "http://localhost:8000/auth/strava/callback")
os.environ.setdefault("FRONTEND_URL", "http://localhost:4321")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("BACKFILL_SECRET", "test-backfill-secret")
