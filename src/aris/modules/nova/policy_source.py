"""Bounded parsing and pure canonical compilation; no live configuration loader."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from aris.core.contracts import DomainError
from aris.modules.nova.policy_schemas import GpuAllocationPolicy

MAX_POLICY_SOURCE_BYTES = 65536


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON values are not supported")


def parse_policy_source(body: bytes) -> GpuAllocationPolicy:
    if not body or len(body) > MAX_POLICY_SOURCE_BYTES:
        raise DomainError("VALIDATION_ERROR", "Policy source exceeds its size budget", 422)
    try:
        value = json.loads(
            body.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant
        )
        return GpuAllocationPolicy.model_validate(value)
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise DomainError("VALIDATION_ERROR", "Invalid policy source", 422) from None


@dataclass(frozen=True)
class CompiledPolicy:
    source: GpuAllocationPolicy
    canonical_json: bytes
    fingerprint: str


def compile_policy(source: GpuAllocationPolicy) -> CompiledPolicy:
    # Revalidate even a constructed/copied model: compiler callers cannot bypass
    # strict source checks with Pydantic's intentionally unchecked APIs.
    validated = GpuAllocationPolicy.model_validate(source.model_dump(mode="json"))
    ordered = tuple(sorted(validated.spec.role_overrides, key=lambda item: -item.priority))
    canonical = validated.model_copy(
        update={"spec": validated.spec.model_copy(update={"role_overrides": ordered})}
    )
    encoded = json.dumps(
        canonical.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(encoded) > MAX_POLICY_SOURCE_BYTES:
        raise DomainError("VALIDATION_ERROR", "Policy source exceeds its size budget", 422)
    return CompiledPolicy(
        source=canonical,
        canonical_json=encoded,
        fingerprint=hashlib.sha256(b"aris.policy.source\x00" + encoded).hexdigest(),
    )
