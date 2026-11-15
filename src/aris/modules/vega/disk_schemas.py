from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.contracts import Contract

DISK_STALE_AFTER_SECONDS = 7200


class DiskVolume(Contract):
    device_id: str = Field(pattern=r"^[0-9]+:[0-9]+$", max_length=64)
    source: str = Field(min_length=1, max_length=512)
    fs_type: str = Field(min_length=1, max_length=128)
    mount_points: list[str] = Field(min_length=1, max_length=64)
    quality: Literal["ok", "error", "unsupported"]
    total_bytes: int | None = Field(default=None, gt=0)
    used_bytes: int | None = Field(default=None, ge=0)
    available_bytes: int | None = Field(default=None, ge=0)
    error: Literal["unavailable", "mount_changed", "invalid_counters", "unsupported"] | None = None

    @field_validator("mount_points")
    @classmethod
    def allowed_mounts(cls, values: list[str]) -> list[str]:
        for value in values:
            if (value != "/" and not value.startswith("/mnt/")) or len(value) > 1024:
                raise ValueError("disk mount must be / or below /mnt/")
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("disk mount contains control characters")
        if len(set(values)) != len(values):
            raise ValueError("duplicate disk mount")
        return values

    @model_validator(mode="after")
    def valid_counters(self) -> "DiskVolume":
        counters = (self.total_bytes, self.used_bytes, self.available_bytes)
        if self.quality == "ok":
            if any(value is None for value in counters) or self.error is not None:
                raise ValueError("valid disk usage requires measured counters")
            assert self.total_bytes is not None
            assert self.used_bytes is not None and self.available_bytes is not None
            if self.used_bytes + self.available_bytes > self.total_bytes:
                raise ValueError("disk used and available exceed total")
        elif any(value is not None for value in counters):
            raise ValueError("unknown disk usage cannot contain current counters")
        return self


class DiskObservation(Contract):
    source: Literal["linux_mountinfo_statvfs"] = "linux_mountinfo_statvfs"
    observed_at: AwareDatetime
    quality: Literal["ok", "partial", "error", "unsupported"]
    volumes: list[DiskVolume] = Field(default_factory=list, max_length=64)
    truncated: bool = False

    @field_validator("observed_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def valid_volumes(self) -> "DiskObservation":
        if len({volume.device_id for volume in self.volumes}) != len(self.volumes):
            raise ValueError("duplicate filesystem device")
        if self.quality == "ok" and (
            not self.volumes or self.truncated or any(v.quality != "ok" for v in self.volumes)
        ):
            raise ValueError("complete disk observation requires all selected volumes")
        return self
