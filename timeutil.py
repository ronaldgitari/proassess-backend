"""
Timestamp serialization helper.

The DB stores naive UTC datetimes (datetime.utcnow()). Plain .isoformat()
emits NO timezone suffix, so a browser's `new Date(str)` interprets it as
LOCAL time — shifting every timestamp by the viewer's UTC offset.

iso_utc() appends 'Z' (the UTC marker) so the frontend converts UTC → the
viewer's local timezone correctly and automatically.
"""
from datetime import datetime, timezone, date


def iso_utc(dt) -> str | None:
    """Serialize a naive-UTC datetime/date to an ISO string marked as UTC ('Z')."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        # Treat naive datetimes as UTC; normalise aware ones to UTC.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    if isinstance(dt, date):
        return dt.isoformat()   # plain date, no tz
    return None
