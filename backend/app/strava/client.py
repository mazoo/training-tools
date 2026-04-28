import httpx

from app.config import settings
from app.strava.rate_limiter import BudgetExhausted, rate_limiter

STRAVA_BASE = "https://www.strava.com/api/v3"


class StravaRateLimitError(Exception):
    def __init__(self, retry_after_s: int = 900):
        self.retry_after_s = retry_after_s


class StravaClient:
    def __init__(self, access_token: str) -> None:
        self.access_token = access_token

    async def _get(self, path: str, params: dict | None = None) -> dict | list:
        try:
            await rate_limiter.acquire()
        except BudgetExhausted:
            raise StravaRateLimitError(retry_after_s=int(rate_limiter.seconds_until_15min_reset()))
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{STRAVA_BASE}{path}",
                headers={"Authorization": f"Bearer {self.access_token}"},
                params=params or {},
                timeout=30.0,
            )
        rate_limiter.sync_from_headers(dict(resp.headers))
        if resp.status_code == 429:
            # Actual 429 from Strava — headers are now synced so next acquire() will stop.
            raise StravaRateLimitError(retry_after_s=int(rate_limiter.seconds_until_15min_reset()))
        resp.raise_for_status()
        return resp.json()

    async def get_athlete(self) -> dict:
        return await self._get("/athlete")

    async def get_starred_segments(self) -> list[dict]:
        results: list[dict] = []
        page = 1
        while True:
            page_data = await self._get(
                "/segments/starred", {"page": page, "per_page": 200}
            )
            if not page_data:
                break
            results.extend(page_data)
            if len(page_data) < 200:
                break
            page += 1
        return results

    async def get_activities(
        self,
        after: int | None = None,
        before: int | None = None,
        per_page: int = 200,
        page: int = 1,
    ) -> list[dict]:
        params: dict = {"per_page": per_page, "page": page}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        return await self._get("/athlete/activities", params)

    async def get_activity(self, activity_id: int) -> dict:
        # include_all_efforts=true ensures all segment efforts are returned,
        # not just PRs — segment metadata (name, grade, latlng) is embedded here.
        return await self._get(f"/activities/{activity_id}", {"include_all_efforts": "true"})

    async def get_segment_efforts(
        self,
        segment_id: int,
        per_page: int = 200,
        page: int = 1,
    ) -> list[dict]:
        return await self._get("/segment_efforts", {"segment_id": segment_id, "per_page": per_page, "page": page})

    async def get_segment(self, segment_id: int) -> dict:
        return await self._get(f"/segments/{segment_id}")

    @staticmethod
    async def exchange_token(code: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://www.strava.com/oauth/token",
                data={
                    "client_id": settings.strava_client_id,
                    "client_secret": settings.strava_client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                },
                timeout=30.0,
            )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    async def refresh_access_token(refresh_token: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://www.strava.com/oauth/token",
                data={
                    "client_id": settings.strava_client_id,
                    "client_secret": settings.strava_client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=30.0,
            )
        resp.raise_for_status()
        return resp.json()
