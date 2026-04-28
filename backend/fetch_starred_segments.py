"""One-off script: fetch starred segments from Strava and save raw JSON."""

import asyncio
import json
import sqlite3
import time
import httpx

DB_PATH = "training_tools.db"
OUT_PATH = "starred_segments_raw.json"
STRAVA_BASE = "https://www.strava.com/api/v3"


def get_token() -> dict:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT athlete_id, access_token, refresh_token, expires_at FROM athlete_tokens LIMIT 1"
    ).fetchone()
    con.close()
    if not row:
        raise RuntimeError("No token found in DB — have you connected Strava?")
    return {"athlete_id": row[0], "access_token": row[1], "refresh_token": row[2], "expires_at": row[3]}


def refresh_token_if_needed(token: dict) -> str:
    if time.time() + 60 < token["expires_at"]:
        return token["access_token"]

    import os
    from dotenv import load_dotenv
    load_dotenv(".env")
    client_id = os.environ["STRAVA_CLIENT_ID"]
    client_secret = os.environ["STRAVA_CLIENT_SECRET"]

    resp = httpx.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE athlete_tokens SET access_token=?, refresh_token=?, expires_at=? WHERE athlete_id=?",
        (data["access_token"], data["refresh_token"], data["expires_at"], token["athlete_id"]),
    )
    con.commit()
    con.close()
    print("Token refreshed.")
    return data["access_token"]


async def fetch_all_starred(access_token: str) -> list[dict]:
    results: list[dict] = []
    page = 1
    async with httpx.AsyncClient() as client:
        while True:
            resp = await client.get(
                f"{STRAVA_BASE}/segments/starred",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"page": page, "per_page": 200},
                timeout=30,
            )
            print(f"  page {page}: HTTP {resp.status_code}  "
                  f"rate-limit usage={resp.headers.get('X-RateLimit-Usage', '?')}")
            resp.raise_for_status()
            page_data = resp.json()
            if not page_data:
                break
            results.extend(page_data)
            if len(page_data) < 200:
                break
            page += 1
    return results


async def main() -> None:
    token = get_token()
    access_token = refresh_token_if_needed(token)

    print("Fetching starred segments…")
    segments = await fetch_all_starred(access_token)
    print(f"Got {len(segments)} segments.")

    with open(OUT_PATH, "w") as f:
        json.dump(segments, f, indent=2)
    print(f"Saved to {OUT_PATH}")

    # Print all top-level keys found across all segment objects
    all_keys: set[str] = set()
    for seg in segments:
        all_keys.update(seg.keys())
    print("\nAll top-level keys in response:")
    for k in sorted(all_keys):
        print(f"  {k}")


asyncio.run(main())
