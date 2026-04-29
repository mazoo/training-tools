import re
from math import atan2, cos, radians, sin, sqrt

_INDOOR_RE = re.compile(r"(?i)(zwift|virtual|indoor|trainer)")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def xom_to_seconds(s: object) -> int | None:
    if not s:
        return None
    try:
        text = str(s).strip()
        # Strava returns bare-seconds format for short segments, e.g. "28s"
        if text.endswith("s") and ":" not in text:
            return int(text[:-1])
        parts = text.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, IndexError):
        return None


def seconds_to_display(s: int | None) -> str | None:
    if s is None:
        return None
    if s < 3600:
        return f"{s // 60}:{s % 60:02d}"
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def is_segment_indoor(segment: dict) -> bool:
    latlng = segment.get("start_latlng") or []
    if not latlng or (len(latlng) == 2 and abs(latlng[0]) < 1e-9 and abs(latlng[1]) < 1e-9):
        return True
    # activity_type is the most reliable signal: Strava sets it to VirtualRide for Zwift etc.
    if segment.get("activity_type") == "VirtualRide":
        return True
    if _INDOOR_RE.search(segment.get("name", "")):
        return True
    return False
