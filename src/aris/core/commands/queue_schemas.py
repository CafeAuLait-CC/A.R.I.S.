from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aris.core.access.schemas import AccessContract, ExternalSubject
from aris.core.commands.schemas import ActorContext, CommandOutcome


class DurableActorContext(ActorContext):
    subject: ExternalSubject


class CommandIntakeContext(DurableActorContext):
    receipt_key: str = Field(min_length=1, max_length=256, repr=False)


class DurableCommandContext(CommandIntakeContext):
    idempotency_key: str = Field(min_length=1, max_length=128, repr=False)


class IntakeLimits(AccessContract):
    lock_timeout_ms: int = Field(gt=0, le=1000, strict=True)
    statement_timeout_ms: int = Field(gt=0, le=1500, strict=True)
    transaction_timeout_ms: int = Field(gt=0, le=2000, strict=True)
    max_queue_delay_seconds: int = Field(gt=0, le=600, strict=True)
    max_pending_commands: int = Field(gt=0, le=10000, strict=True)

    @model_validator(mode="after")
    def ordered_timeouts(self) -> "IntakeLimits":
        if not self.lock_timeout_ms < self.statement_timeout_ms < self.transaction_timeout_ms:
            raise ValueError("lock, statement and transaction budgets must increase")
        return self


class CommandWorkerLimits(AccessContract):
    lock_timeout_ms: int = Field(gt=0, le=10000, strict=True)
    statement_timeout_ms: int = Field(gt=0, le=20000, strict=True)
    transaction_timeout_ms: int = Field(gt=0, le=30000, strict=True)
    lease_seconds: int = Field(ge=2, le=600, strict=True)
    max_attempts: int = Field(ge=1, le=20, strict=True)
    retry_base_seconds: int = Field(ge=1, le=60, strict=True)

    @model_validator(mode="after")
    def ordered_timeouts(self) -> "CommandWorkerLimits":
        if (
            not self.lock_timeout_ms
            < self.statement_timeout_ms
            < self.transaction_timeout_ms
            < self.lease_seconds * 1000
        ):
            raise ValueError("lease must exceed the ordered transaction budgets")
        return self


class IntakeResult(AccessContract):
    receipt_id: UUID
    disposition: Literal["accepted", "rejected"]
    duplicate: bool
    outcome: CommandOutcome | None = None
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[A-Z][A-Z0-9_]+$")
    error_status: int | None = Field(default=None, ge=400, le=599)
    retryable: bool = False

    @model_validator(mode="after")
    def explicit_outcome(self) -> "IntakeResult":
        if self.disposition == "accepted":
            if (
                self.outcome is None
                or self.error_code is not None
                or self.error_status is not None
                or self.retryable
            ):
                raise ValueError("accepted receipt requires an outcome without error")
        elif self.outcome is not None or self.error_code is None or self.error_status is None:
            raise ValueError("rejected receipt requires only an error")
        return self


class CommandClaim(AccessContract):
    job_id: UUID
    command_id: UUID
    lease_token: UUID
    lease_expires_at: AwareDatetime
    attempt: int = Field(ge=1)


class CommandWorkResult(AccessContract):
    job_id: UUID
    command_id: UUID
    state: Literal["succeeded", "failed", "expired", "cancelled", "retry", "lost_claim"]
    code: str | None = None


class WithdrawCommand(AccessContract):
    command_id: UUID
