"""Editor contracts and the policy-name boundary of the existing command gateway."""

from typing import Literal
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from aris.core.access.schemas import AccessContract
from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.gateway_schemas import UserCommandInput
from aris.core.commands.queue_schemas import IntakeResult, WithdrawCommand
from aris.core.commands.schemas import CommandOutcome, CommandReview, ConfirmCommand
from aris.core.contracts import DomainError
from aris.interfaces.commands.auth import UserCommandAuthenticator
from aris.interfaces.commands.service import UserCommandSurface
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference, RecordPolicyDraft
from aris.modules.nova.policy_feedback_schemas import RecordPolicyFeedback
from aris.modules.nova.policy_history_record_schemas import (
    HistoricalReviewConfiguration,
    RecordHistoricalPolicyReview,
)
from aris.modules.nova.policy_material_schemas import RecordPolicyReviewMaterial
from aris.modules.nova.policy_review import PolicyDifference


class PolicyDraftInput(AccessContract):
    # The raw text reaches the strict server parser, including duplicate JSON keys.
    source_text: str = Field(min_length=1, max_length=24576, repr=False)
    reason: str = Field(min_length=1, max_length=256, repr=False)
    base: PolicySourceReference | None = None
    expected_base_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def bound_base(self) -> "PolicyDraftInput":
        if (self.base is None) != (self.expected_base_fingerprint is None):
            raise ValueError("a base comparison requires its exact source fingerprint")
        return self


class PolicyDraftValidation(AccessContract):
    validated_draft_only: Literal[True] = True
    publication_performed: Literal[False] = False
    activation_performed: Literal[False] = False
    draft: RecordPolicyDraft = Field(repr=False)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    inline_bytes: int = Field(ge=1, le=16384)
    difference: PolicyDifference | None = None
    existing_equivalent: bool = False


class PolicyDraftCommandSurface(UserCommandSurface):
    """Only expose draft intents whose policy name is configured in this interface.

    Names are an interface boundary, not a replacement for current Core RBAC.
    The Worker still uses the original domain action for admitted intents.
    """

    def __init__(
        self,
        gateway: UserCommandGateway,
        authentication: UserCommandAuthenticator,
        policy_names: tuple[str, ...],
        *,
        material_implementation_ref: str | None = None,
        feedback_writing: bool = False,
        historical_review_storage: HistoricalReviewConfiguration | None = None,
    ) -> None:
        super().__init__(gateway, authentication)
        allowed = {"policy.record_draft"}
        if material_implementation_ref is not None:
            allowed.add("policy.record_review_material")
        if feedback_writing:
            allowed.add("policy.record_feedback")
        if historical_review_storage is not None:
            allowed.add("policy.record_historical_review")
        if gateway.allowed != frozenset(allowed):
            raise ValueError("policy interface commands must match its explicit configuration")
        if not 1 <= len(policy_names) <= 32 or len(set(policy_names)) != len(policy_names):
            raise ValueError("draft policy names must be explicit and unique")
        for name in policy_names:
            PolicySourceReference(name=name, version=1)
        self.policy_names = policy_names
        self.material_implementation_ref = material_implementation_ref
        self.feedback_writing = feedback_writing
        self.historical_review_storage = historical_review_storage

    def _name(self, request: UserCommandInput) -> None:
        if request.command not in self.gateway.allowed:
            raise DomainError("PERMISSION_DENIED", "Command is outside this draft interface", 403)
        try:
            if request.command == "policy.record_draft":
                name = RecordPolicyDraft.model_validate(request.parameters).source.metadata.name
            elif request.command == "policy.record_feedback":
                name = RecordPolicyFeedback.model_validate(request.parameters).source.name
            elif request.command == "policy.record_historical_review":
                name = RecordHistoricalPolicyReview.model_validate(
                    request.parameters
                ).query.policy.name
            else:
                name = RecordPolicyReviewMaterial.model_validate(request.parameters).source.name
        except ValidationError:
            raise DomainError("VALIDATION_ERROR", "Invalid policy draft intent", 422) from None
        if name not in self.policy_names:
            raise DomainError("PERMISSION_DENIED", "Policy name is outside this interface", 403)

    def submit(
        self, authorization: str | None, request_id: str, key: str, request: UserCommandInput
    ) -> IntakeResult:
        self.actor(authorization, request_id)
        self._name(request)
        return super().submit(authorization, request_id, key, request)

    def review(self, authorization: str | None, request_id: str, command_id: UUID) -> CommandReview:
        result = super().review(authorization, request_id, command_id)
        self._name(
            UserCommandInput(command=result.request.command, parameters=result.request.parameters)
        )
        return result

    def inspect(
        self, authorization: str | None, request_id: str, command_id: UUID
    ) -> CommandOutcome:
        return self.review(authorization, request_id, command_id).outcome

    def confirm(
        self, authorization: str | None, request_id: str, key: str, request: ConfirmCommand
    ) -> IntakeResult:
        self.review(authorization, request_id, request.command_id)
        return super().confirm(authorization, request_id, key, request)

    def withdraw(
        self, authorization: str | None, request_id: str, key: str, request: WithdrawCommand
    ) -> IntakeResult:
        self.review(authorization, request_id, request.command_id)
        return super().withdraw(authorization, request_id, key, request)
