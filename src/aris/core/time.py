from datetime import UTC, datetime


def utc(value: datetime) -> datetime:
    """Restore SQLite's missing zone; preserve PostgreSQL's UTC instant."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
