from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator

from aris.core.access.schemas import AccessContract

Username = Annotated[str, Field(min_length=3, max_length=64, pattern=r"^[a-z][a-z0-9_.-]+$")]


def normalize_username(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("enter a valid login username")
    return value.strip().lower()


class PasswordFields(AccessContract):
    username: Username
    password: SecretStr = Field(min_length=15, max_length=128, repr=False)

    @field_validator("username", mode="before")
    @classmethod
    def canonical_username(cls, value: str) -> str:
        return normalize_username(value)


class PasswordInput(PasswordFields):
    remember: bool = Field(default=True, strict=True)


class InvitationInput(AccessContract):
    username: Username
    purpose: Literal["activate", "reset"] = "activate"

    @field_validator("username", mode="before")
    @classmethod
    def canonical_username(cls, value: str) -> str:
        return normalize_username(value)


class ActivatePasswordInput(PasswordInput):
    challenge_id: UUID
    code: SecretStr = Field(min_length=6, max_length=6, repr=False)

    @field_validator("code")
    @classmethod
    def numeric_code(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().isascii() or not value.get_secret_value().isdigit():
            raise ValueError("enter the six-digit invitation code")
        return value


class SetOwnPasswordInput(PasswordFields):
    current_password: SecretStr | None = Field(default=None, max_length=128, repr=False)


class InvitationReceipt(AccessContract):
    challenge_id: UUID
    expires_in: int = 600
    message: str = "If this username is eligible, an invitation code is sent to its Slack account."


class PasswordAccountSummary(AccessContract):
    username: str | None = None
    password_ready: bool = False
