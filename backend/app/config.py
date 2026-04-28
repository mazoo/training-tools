from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("home_lat", "home_lng", mode="before")
    @classmethod
    def empty_str_to_none(cls, v: object) -> object:
        return None if v == "" else v

    strava_client_id: str
    strava_client_secret: str
    strava_redirect_uri: str          # e.g. http://localhost:8000/auth/strava/callback
    frontend_url: str                 # e.g. http://localhost:4321
    database_url: str = "sqlite+aiosqlite:///./training_tools.db"
    secret_key: str                   # random secret for session signing
    backfill_secret: str              # random secret for POST /api/internal/daily-backfill
    home_lat: float | None = None
    home_lng: float | None = None


settings = Settings()
