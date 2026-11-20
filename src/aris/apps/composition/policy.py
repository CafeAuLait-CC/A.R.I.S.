"""Explicit development composition for confirmed immutable draft storage only."""

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from pydantic import Field, SecretStr, model_validator
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.schemas import AccessContract
from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.gateway_schemas import GatewayLimits
from aris.core.commands.intake import CommandIntakeService
from aris.core.commands.queue_schemas import CommandWorkerLimits, IntakeLimits
from aris.core.commands.service import CommandService, DatabaseCommand
from aris.core.commands.worker import CommandWorker
from aris.interfaces.commands.auth import BoundSubjectTokens
from aris.interfaces.policy.drafts import PolicyDraftCommandSurface
from aris.modules.lyra.quota_evidence_service import HistoricalQuotaEvidenceService
from aris.modules.nova.policy_catalog_commands import RecordPolicyDraftCommand
from aris.modules.nova.policy_catalog_service import PolicyCatalogService
from aris.modules.nova.policy_feedback_commands import RecordPolicyFeedbackCommand
from aris.modules.nova.policy_feedback_service import PolicyFeedbackService
from aris.modules.nova.policy_history_record_commands import RecordHistoricalPolicyReviewCommand
from aris.modules.nova.policy_history_record_schemas import HistoricalReviewConfiguration
from aris.modules.nova.policy_history_record_service import PolicyHistoricalReviewService
from aris.modules.nova.policy_material_commands import RecordPolicyReviewMaterialCommand
from aris.modules.nova.policy_material_schemas import ImplementationReference
from aris.modules.nova.policy_material_service import PolicyReviewMaterialService
from aris.modules.nova.quota_history_service import HistoricalQuotaAssessmentService


class PolicyDraftRuntimeConfig(AccessContract):
    namespace: IdentityNamespace
    confirmation_key: SecretStr = Field(min_length=32, max_length=512, repr=False)
    confirmation_key_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    confirmation_ttl_seconds: int = Field(ge=30, le=900, strict=True)
    gateway_limits: GatewayLimits
    intake_limits: IntakeLimits
    worker_limits: CommandWorkerLimits
    poll_seconds: float = Field(default=1, ge=0.1, le=60)
    review_material_implementation_ref: ImplementationReference | None = None
    feedback_writing: bool = Field(default=False, strict=True)
    historical_review_storage: HistoricalReviewConfiguration | None = None

    @model_validator(mode="after")
    def explicit_workspace(self) -> "PolicyDraftRuntimeConfig":
        if self.namespace.provider != "slack":
            raise ValueError("the development draft interface requires one Slack workspace")
        return self


def validate_policy_draft_configuration(
    config: PolicyDraftRuntimeConfig, auth: BoundSubjectTokens, read_token: SecretStr
) -> None:
    auth.ensure_distinct_credentials([config.confirmation_key])
    if hmac.compare_digest(
        config.confirmation_key.get_secret_value().encode(),
        read_token.get_secret_value().encode(),
    ):
        raise ValueError("the confirmation key must be distinct from the read credential")
    if any(
        (subject.provider, subject.namespace)
        != (config.namespace.provider, config.namespace.namespace)
        for _, subject in auth.bindings
    ):
        raise ValueError("all draft credentials must belong to the configured workspace")


@dataclass(frozen=True)
class PolicyDraftComponents:
    user_api: PolicyDraftCommandSurface
    worker: CommandWorker
    historical_reviews: PolicyHistoricalReviewService | None = None


def build_policy_draft_components(
    sessions: sessionmaker[Session],
    auth: BoundSubjectTokens,
    policy_names: tuple[str, ...],
    config: PolicyDraftRuntimeConfig,
    *,
    clock: Callable[[], datetime] | None = None,
) -> PolicyDraftComponents:
    catalog = PolicyCatalogService(sessions, clock=clock)
    actions: list[DatabaseCommand] = [RecordPolicyDraftCommand(catalog)]
    allowed = ["policy.record_draft"]
    historical_reviews = None
    if config.historical_review_storage is not None:
        historical_reviews = PolicyHistoricalReviewService(
            catalog,
            HistoricalQuotaAssessmentService(catalog, HistoricalQuotaEvidenceService(sessions)),
            config.historical_review_storage,
            writing_enabled=True,
        )
        actions.append(RecordHistoricalPolicyReviewCommand(historical_reviews))
        allowed.append("policy.record_historical_review")
    if config.feedback_writing:
        actions.append(
            RecordPolicyFeedbackCommand(PolicyFeedbackService(catalog, writing_enabled=True))
        )
        allowed.append("policy.record_feedback")
    if config.review_material_implementation_ref is not None:
        actions.append(
            RecordPolicyReviewMaterialCommand(
                PolicyReviewMaterialService(
                    catalog, implementation_ref=config.review_material_implementation_ref
                )
            )
        )
        allowed.append("policy.record_review_material")
    commands = CommandService(
        sessions,
        actions,
        confirmation_key=config.confirmation_key,
        confirmation_ttl_seconds=config.confirmation_ttl_seconds,
        clock=clock,
    )
    intake = CommandIntakeService(
        commands,
        config.namespace,
        config.intake_limits,
        confirmation_key_id=config.confirmation_key_id,
    )
    gateway = UserCommandGateway(intake, tuple(allowed), config.gateway_limits)
    return PolicyDraftComponents(
        user_api=PolicyDraftCommandSurface(
            gateway,
            auth,
            policy_names,
            material_implementation_ref=config.review_material_implementation_ref,
            feedback_writing=config.feedback_writing,
            historical_review_storage=config.historical_review_storage,
        ),
        worker=CommandWorker(intake, config.worker_limits),
        historical_reviews=historical_reviews,
    )
