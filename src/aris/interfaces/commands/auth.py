import hashlib
import hmac
from collections.abc import Sequence
from typing import Protocol

from pydantic import Field, SecretStr

from aris.core.access.schemas import AccessContract, ExternalSubject
from aris.core.contracts import DomainError


class UserTokenBinding(AccessContract):
    subject: ExternalSubject
    token: SecretStr = Field(min_length=32, max_length=256, repr=False)


class UserCommandAuthenticator(Protocol):
    @property
    def development_only(self) -> bool: ...
    def ensure_distinct_credentials(self, reserved_tokens: Sequence[SecretStr]) -> None: ...
    def authenticate(self, authorization: str | None) -> ExternalSubject: ...


class BoundSubjectTokens:
    """Explicit development credentials; current platform grants remain required."""

    development_only = True

    def __init__(
        self, bindings: Sequence[UserTokenBinding], *, reserved_tokens: Sequence[SecretStr]
    ) -> None:
        if not 1 <= len(bindings) <= 128:
            raise ValueError("explicit user credential bindings are required")
        self.bindings: list[tuple[bytes, ExternalSubject]] = []
        reserved = {
            hashlib.sha256(token.get_secret_value().encode()).digest() for token in reserved_tokens
        }
        seen = set()
        for binding in bindings:
            raw = binding.token.get_secret_value()
            if (
                not raw.isascii()
                or not raw.isprintable()
                or any(character.isspace() for character in raw)
            ):
                raise ValueError("user credentials must be non-whitespace ASCII")
            digest = hashlib.sha256(raw.encode()).digest()
            if digest in seen or digest in reserved:
                raise ValueError("user credential is duplicate or reserved")
            seen.add(digest)
            self.bindings.append((digest, binding.subject))

    def ensure_distinct_credentials(self, reserved_tokens: Sequence[SecretStr]) -> None:
        reserved = {
            hashlib.sha256(token.get_secret_value().encode()).digest() for token in reserved_tokens
        }
        if any(digest in reserved for digest, _ in self.bindings):
            raise ValueError("user credential is reserved by another interface")

    def authenticate(self, authorization: str | None) -> ExternalSubject:
        if authorization is None or len(authorization) > 300:
            raise DomainError("PERMISSION_DENIED", "User authentication is required", 401)
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0] != "Bearer" or not parts[1]:
            raise DomainError("PERMISSION_DENIED", "User authentication is required", 401)
        candidate = hashlib.sha256(parts[1].encode()).digest()
        matched = None
        for expected, subject in self.bindings:
            if hmac.compare_digest(candidate, expected):
                matched = subject
        if matched is None:
            raise DomainError("PERMISSION_DENIED", "User authentication is required", 401)
        return matched
