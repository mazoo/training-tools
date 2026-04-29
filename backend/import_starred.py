"""
One-off backfill: import starred_segments_raw.json into segment_enrichment
and athlete_segment_profile.

Run from backend/ after fetching the JSON:
    uv run python import_starred.py

Safe to re-run; all writes are INSERT OR IGNORE + UPDATE (idempotent).
The script adds any missing columns itself, so it can run before or after
the first server start.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone

DB_PATH = "training_tools.db"
JSON_PATH = "starred_segments_raw.json"

_INDOOR_RE = re.compile(r"(?i)(zwift|virtual|indoor|trainer)")

# New columns that may not exist yet on older DBs.
_ENRICHMENT_COLS = [
    ("end_lat", "REAL"),
    ("end_lng", "REAL"),
    ("state", "TEXT"),
    ("elevation_high", "REAL"),
    ("elevation_low", "REAL"),
    ("activity_type", "TEXT"),
    ("hazardous", "BOOLEAN"),
    ("kom_time_checked_at", "DATETIME"),
]
_PROFILE_COLS = [
    ("is_kom", "BOOLEAN DEFAULT 0"),
    ("pr_time_s", "INTEGER"),
    ("pr_activity_id", "INTEGER"),
    ("pr_date", "DATETIME"),
    ("starred_date", "DATETIME"),
]


def _ensure_columns(con: sqlite3.Connection) -> None:
    for col, col_type in _ENRICHMENT_COLS:
        try:
            con.execute(f"ALTER TABLE segment_enrichment ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    for col, col_type in _PROFILE_COLS:
        try:
            con.execute(f"ALTER TABLE athlete_segment_profile ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    con.commit()


def _is_indoor(seg: dict) -> bool:
    latlng = seg.get("start_latlng") or []
    if not latlng or (len(latlng) == 2 and abs(latlng[0]) < 1e-9 and abs(latlng[1]) < 1e-9):
        return True
    if seg.get("activity_type") == "VirtualRide":
        return True
    return bool(_INDOOR_RE.search(seg.get("name", "")))


def _latlng(raw: object) -> tuple:
    coords = raw if isinstance(raw, list) else []
    return (coords[0], coords[1]) if len(coords) == 2 else (None, None)


def main() -> None:
    with open(JSON_PATH) as f:
        segments = json.load(f)

    con = sqlite3.connect(DB_PATH)
    _ensure_columns(con)

    row = con.execute("SELECT athlete_id FROM athlete_tokens LIMIT 1").fetchone()
    if not row:
        print("No athlete token found in DB — connect Strava first.")
        return
    athlete_id = row[0]
    now = datetime.now(timezone.utc).isoformat()

    enrichment_updated = 0
    profile_updated = 0

    for seg in segments:
        seg_id = seg["id"]
        name = seg.get("name", "")
        indoor = 1 if _is_indoor(seg) else 0

        start_lat, start_lng = _latlng(seg.get("start_latlng"))
        end_lat, end_lng = _latlng(seg.get("end_latlng"))

        # segment_enrichment — geometry/metadata only.
        # cached_at and kom_time_s are intentionally excluded from the UPDATE
        # so an existing fresh KOM time is not overwritten.
        con.execute(
            """
            INSERT INTO segment_enrichment
              (segment_id, segment_name, distance_m, avg_grade_pct, max_grade_pct,
               start_lat, start_lng, end_lat, end_lng,
               city, country, state, climb_category,
               elevation_high, elevation_low, activity_type, hazardous,
               kom_time_s, cached_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
            ON CONFLICT(segment_id) DO UPDATE SET
              segment_name   = excluded.segment_name,
              distance_m     = excluded.distance_m,
              avg_grade_pct  = excluded.avg_grade_pct,
              max_grade_pct  = excluded.max_grade_pct,
              start_lat      = excluded.start_lat,
              start_lng      = excluded.start_lng,
              end_lat        = excluded.end_lat,
              end_lng        = excluded.end_lng,
              city           = excluded.city,
              country        = excluded.country,
              state          = excluded.state,
              climb_category = excluded.climb_category,
              elevation_high = excluded.elevation_high,
              elevation_low  = excluded.elevation_low,
              activity_type  = excluded.activity_type,
              hazardous      = excluded.hazardous
            """,
            (
                seg_id, name,
                seg.get("distance"), seg.get("average_grade"), seg.get("maximum_grade"),
                start_lat, start_lng, end_lat, end_lng,
                seg.get("city"), seg.get("country"), seg.get("state"),
                seg.get("climb_category"),
                seg.get("elevation_high"), seg.get("elevation_low"),
                seg.get("activity_type"),
                1 if seg.get("hazardous") else 0,
                now,
            ),
        )
        enrichment_updated += con.execute("SELECT changes()").fetchone()[0]

        # athlete_segment_profile — athlete-specific PR and starred fields.
        pr = seg.get("athlete_pr_effort") or {}
        pr_time_s = pr.get("elapsed_time")
        pr_activity_id = pr.get("activity_id")
        pr_date = pr.get("start_date")
        is_kom = 1 if pr.get("is_kom") else 0
        starred_date = seg.get("starred_date")

        con.execute(
            """
            INSERT INTO athlete_segment_profile
              (athlete_id, segment_id, segment_name, is_starred, is_indoor,
               times_ridden, top10_seen, podium_seen, updated_at,
               is_kom, pr_time_s, pr_activity_id, pr_date, starred_date)
            VALUES (?,?,?,1,?,0,0,0,?,?,?,?,?,?)
            ON CONFLICT(athlete_id, segment_id) DO UPDATE SET
              is_starred    = 1,
              is_indoor     = excluded.is_indoor,
              is_kom        = excluded.is_kom,
              pr_time_s     = excluded.pr_time_s,
              pr_activity_id = excluded.pr_activity_id,
              pr_date       = excluded.pr_date,
              starred_date  = excluded.starred_date
            """,
            (
                athlete_id, seg_id, name, indoor, now,
                is_kom, pr_time_s, pr_activity_id, pr_date, starred_date,
            ),
        )
        profile_updated += con.execute("SELECT changes()").fetchone()[0]

    con.commit()
    con.close()

    print(f"Processed {len(segments)} segments for athlete_id={athlete_id}")
    print(f"  segment_enrichment rows touched:     {enrichment_updated}")
    print(f"  athlete_segment_profile rows touched: {profile_updated}")

    # Summary stats
    con2 = sqlite3.connect(DB_PATH)
    kom_count = con2.execute(
        "SELECT COUNT(*) FROM athlete_segment_profile WHERE athlete_id=? AND is_kom=1",
        (athlete_id,),
    ).fetchone()[0]
    virtual_count = con2.execute(
        "SELECT COUNT(*) FROM segment_enrichment WHERE activity_type='VirtualRide'"
    ).fetchone()[0]
    con2.close()

    print(f"  Segments where athlete currently holds KOM: {kom_count}")
    print(f"  VirtualRide (indoor) segments:              {virtual_count}")


main()
