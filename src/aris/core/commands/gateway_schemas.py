from pydantic import Field, JsonValue, model_validator

from aris.core.access.schemas import AccessContract, ExternalSubject, PermissionName


class ExternalCommandActor(AccessContract):
    # Subject comes only from a trusted authentication adapter.
    subject: ExternalSubject
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class ExternalCommandContext(ExternalCommandActor):
    receipt_key: str = Field(min_length=1, max_length=256, repr=False)


class ExternalCommandSubmit(ExternalCommandContext):
    idempotency_key: str = Field(min_length=1, max_length=128, repr=False)


class UserCommandInput(AccessContract):
    command: PermissionName
    parameters: dict[str, JsonValue] = Field(default_factory=dict, repr=False)


class GatewayLimits(AccessContract):
    lock_timeout_ms: int = Field(gt=0, le=100, strict=True)
    statement_timeout_ms: int = Field(gt=0, le=200, strict=True)
    transaction_timeout_ms: int = Field(gt=0, le=250, strict=True)

    @model_validator(mode="after")
    def ordered_budgets(self) -> "GatewayLimits":
        if not self.lock_timeout_ms < self.statement_timeout_ms < self.transaction_timeout_ms:
            raise ValueError("gateway metadata budgets must increase")
        return self
