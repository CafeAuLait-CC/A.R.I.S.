"""Opt-in history selection: the authenticated owner and configured methods are server-bound."""

from datetime import timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from aris.core.access.schemas import AccessContract, AccessRequest, AccessScope
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.quota_evidence_schemas import (
    HistoricalQuotaEvidence,
    QuotaEvidenceListQuery,
    QuotaEvidencePage,
    QuotaEvidenceSelection,
)
from aris.modules.lyra.quota_evidence_service import HistoricalQuotaEvidenceService
from aris.modules.lyra.window_reader_schemas import WindowPageCursor
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_history_record_schemas import (
    RecordHistoricalPolicyReview,
    StoredHistoricalPolicyReview,
    canonical_history_record,
    historical_context_fingerprint,
)
from aris.modules.nova.policy_material_schemas import ImplementationReference
from aris.modules.nova.policy_schemas import PolicyName, QuotaLimit
from aris.modules.nova.policy_simulation import ExactGpuSeconds
from aris.modules.nova.policy_units import PolicyTimeWindow
from aris.modules.nova.quota_history_schemas import (
    HistoricalPolicyNode,
    HistoricalQuotaAssessment,
    HistoricalQuotaQuery,
    HistoricalQuotaReview,
)
from aris.modules.nova.quota_history_service import HistoricalQuotaAssessmentService


class PolicyHistoryWindowsInput(AccessContract):
    node_id: str = Field(min_length=1, max_length=128)
    start_at: AwareDatetime
    end_at: AwareDatetime
    before: WindowPageCursor | None = None
    limit: int = Field(default=8, ge=1, le=16, strict=True)

    @model_validator(mode="after")
    def bounded(self) -> "PolicyHistoryWindowsInput":
        if not timedelta(0) < self.end_at - self.start_at <= timedelta(days=366):
            raise ValueError("historical directory requires a bounded positive interval")
        return self


class PolicyHistoryAssessmentInput(AccessContract):
    reference: PolicySourceReference
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    node_id: str = Field(min_length=1, max_length=128)
    snapshot_id: UUID
    expected_snapshot_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    period_at: AwareDatetime
    assumed_roles: tuple[PolicyName, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def unique_roles(self) -> "PolicyHistoryAssessmentInput":
        if len(set(self.assumed_roles)) != len(self.assumed_roles):
            raise ValueError("historical role assumptions must be unique")
        return self


class ExactSecondsView(AccessContract):
    numerator: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    denominator: str = Field(pattern=r"^[1-9][0-9]*$")

    @classmethod
    def of(cls, value: ExactGpuSeconds | None) -> "ExactSecondsView | None":
        return (
            cls(numerator=str(value.numerator), denominator=str(value.denominator))
            if value is not None
            else None
        )


class PolicyHistoryReviewInput(PolicyHistoryAssessmentInput):
    expected_implementation_ref: ImplementationReference


class PolicyHistoryRecordInput(PolicyHistoryReviewInput):
    expected_context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=256)

    @field_validator("reason")
    @classmethod
    def explicit_reason(cls, value: str) -> str:
        return RecordHistoricalPolicyReview.explicit_reason(value)


class PolicyHistoryAssessmentView(AccessContract):
    historical_only: Literal[True] = True
    booking_authorization: Literal[False] = False
    policy_activated: Literal[False] = False
    reservation_precharge: Literal[False] = False
    policy: PolicySourceReference
    policy_fingerprint: str
    selection: QuotaEvidenceSelection
    period: PolicyTimeWindow
    sampled_until: AwareDatetime
    captured_at: AwareDatetime
    snapshot_created_at: AwareDatetime
    evidence_fingerprint: str
    state: Literal["within_limit", "exhausted", "unlimited", "not_evaluated"]
    reason: str
    assumed_roles: tuple[PolicyName, ...]
    selected_assumed_role: str | None
    limit: QuotaLimit | None
    used_gpu_seconds: ExactSecondsView | None
    remaining_gpu_seconds: ExactSecondsView | None

    @classmethod
    def of(cls, value: HistoricalQuotaAssessment) -> "PolicyHistoryAssessmentView":
        return cls(
            policy=value.query.policy,
            policy_fingerprint=value.query.expected_policy_fingerprint,
            selection=value.query.usage,
            period=value.period,
            sampled_until=value.sampled_until,
            captured_at=value.captured_at,
            snapshot_created_at=value.snapshot_created_at,
            evidence_fingerprint=value.evidence_fingerprint,
            state=value.state,
            reason=value.reason,
            assumed_roles=value.query.assumed_roles,
            selected_assumed_role=value.selected_assumed_role,
            limit=value.limit,
            used_gpu_seconds=ExactSecondsView.of(value.used_gpu_seconds),
            remaining_gpu_seconds=ExactSecondsView.of(value.remaining_gpu_seconds),
        )


class HistoricalModelView(AccessContract):
    model: str
    observed_microseconds: str = Field(pattern=r"^(0|[1-9][0-9]*)$")


class HistoricalEvidenceView(AccessContract):
    window_start_at: AwareDatetime
    window_end_at: AwareDatetime
    coverage: Literal["complete", "partial", "unknown"]
    source_prefix_complete: bool = Field(strict=True)
    source_ceiling: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    ledger_revision: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    ledger_fingerprint: str | None
    identity_revision: str | None
    configuration_fingerprint: str
    timing_fingerprint: str
    recording_start_at: AwareDatetime
    total_usage_microseconds: str | None
    models: tuple[HistoricalModelView, ...] | None

    @classmethod
    def of(cls, value: HistoricalQuotaEvidence) -> "HistoricalEvidenceView":
        return cls(
            window_start_at=value.window.start_at,
            window_end_at=value.window.end_at,
            coverage=value.coverage,
            source_prefix_complete=value.source_prefix_complete,
            source_ceiling=str(value.source_ceiling),
            ledger_revision=str(value.ledger.revision),
            ledger_fingerprint=value.ledger.fingerprint,
            identity_revision=(
                str(value.identity_revision) if value.identity_revision is not None else None
            ),
            configuration_fingerprint=value.configuration_fingerprint,
            timing_fingerprint=value.timing_fingerprint,
            recording_start_at=value.recording_start_at,
            total_usage_microseconds=(
                str(value.total_usage_microseconds)
                if value.total_usage_microseconds is not None
                else None
            ),
            models=(
                tuple(
                    HistoricalModelView(
                        model=item.model, observed_microseconds=str(item.observed_microseconds)
                    )
                    for item in value.models
                )
                if value.models is not None
                else None
            ),
        )


class PolicyHistoryReviewView(AccessContract):
    review_context_only: Literal[True] = True
    implementation_provenance: Literal["configured_runtime_reference"] = (
        "configured_runtime_reference"
    )
    implementation_ref: ImplementationReference
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    assessment: PolicyHistoryAssessmentView
    evidence: HistoricalEvidenceView
    # Retain the exact domain JSON as text; JavaScript must not round large integers.
    canonical_review_json: str = Field(min_length=1, max_length=2_097_152)

    @property
    def review(self) -> HistoricalQuotaReview:
        return HistoricalQuotaReview.model_validate_json(self.canonical_review_json)

    @model_validator(mode="after")
    def coherent(self) -> "PolicyHistoryReviewView":
        original = self.review
        if (
            self.assessment != PolicyHistoryAssessmentView.of(original.assessment)
            or self.evidence != HistoricalEvidenceView.of(original.evidence)
            or (
                self.context_fingerprint
                != historical_context_fingerprint(original, self.implementation_ref)
            )
        ):
            raise ValueError("historical review display differs from its original evidence")
        return self

    @classmethod
    def of(cls, value: HistoricalQuotaReview, implementation_ref: str) -> "PolicyHistoryReviewView":
        return cls(
            implementation_ref=implementation_ref,
            context_fingerprint=historical_context_fingerprint(value, implementation_ref),
            assessment=PolicyHistoryAssessmentView.of(value.assessment),
            evidence=HistoricalEvidenceView.of(value.evidence),
            canonical_review_json=value.model_dump_json(),
        )


class PolicyStoredHistoryView(AccessContract):
    id: UUID
    created_at: AwareDatetime
    created_by: UUID
    command_id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str
    context: PolicyHistoryReviewView
    canonical_record_json: str = Field(min_length=1, max_length=2_097_152)

    @classmethod
    def of(cls, value: StoredHistoricalPolicyReview) -> "PolicyStoredHistoryView":
        return cls(
            id=value.id,
            created_at=value.created_at,
            created_by=value.created_by,
            command_id=value.command_id,
            fingerprint=value.fingerprint,
            reason=value.request.reason,
            context=PolicyHistoryReviewView.of(
                value.review, value.request.expected_implementation_ref
            ),
            canonical_record_json=canonical_history_record(value),
        )

    @model_validator(mode="after")
    def coherent(self) -> "PolicyStoredHistoryView":
        original = StoredHistoricalPolicyReview.model_validate_json(self.canonical_record_json)
        if (
            self.id,
            self.created_at,
            self.created_by,
            self.command_id,
            self.fingerprint,
            self.reason,
        ) != (
            original.id,
            original.created_at,
            original.created_by,
            original.command_id,
            original.fingerprint,
            original.request.reason,
        ) or self.context != PolicyHistoryReviewView.of(
            original.review, original.request.expected_implementation_ref
        ):
            raise ValueError("historical record display differs from its original")
        return self


class PolicyHistoryRecordValidation(AccessContract):
    historical_only: Literal[True] = True
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    request: RecordHistoricalPolicyReview = Field(repr=False)
    context: PolicyHistoryReviewView


class PolicyHistoricalUsage:
    def __init__(
        self,
        assessments: HistoricalQuotaAssessmentService,
        reader: HistoricalQuotaEvidenceService,
        nodes: tuple[HistoricalPolicyNode, ...],
    ) -> None:
        if not 1 <= len(nodes) <= 8 or len({node.node_id for node in nodes}) != len(nodes):
            raise ValueError("historical policy reads require explicit unique node methods")
        if assessments.usage is not reader:
            raise ValueError("historical policy reads require one evidence boundary")
        self.assessments, self.reader, self.nodes = assessments, reader, nodes

    def _node(self, node_id: str) -> HistoricalPolicyNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise DomainError("PERMISSION_DENIED", "Historical node is outside this interface", 403)

    def accessible_nodes(self, session: Session, actor: ActorContext) -> tuple[str, ...]:
        result = []
        for node in self.nodes:
            try:
                self.assessments.catalog.access.require_in_transaction(
                    session,
                    AccessRequest(
                        user_id=actor.actor_id,
                        permission="usage.read_own",
                        scope=AccessScope(type="node", id=node.node_id),
                    ),
                )
                result.append(node.node_id)
            except DomainError as error:
                if error.code != "PERMISSION_DENIED":
                    raise
        return tuple(result)

    def windows(self, actor: ActorContext, query: PolicyHistoryWindowsInput) -> QuotaEvidencePage:
        self._node(query.node_id)
        return self.reader.list(
            actor, QuotaEvidenceListQuery(**query.model_dump(), owner_id=actor.actor_id)
        )

    def _query(
        self, actor: ActorContext, query: PolicyHistoryAssessmentInput
    ) -> HistoricalQuotaQuery:
        node = self._node(query.node_id)
        return HistoricalQuotaQuery(
            policy=query.reference,
            expected_policy_fingerprint=query.expected_fingerprint,
            usage=QuotaEvidenceSelection(
                snapshot_id=query.snapshot_id,
                expected_snapshot_fingerprint=query.expected_snapshot_fingerprint,
                node_id=query.node_id,
                owner_id=actor.actor_id,
            ),
            expected_configuration_fingerprint=node.expected_configuration_fingerprint,
            expected_timing_fingerprint=node.expected_timing_fingerprint,
            period_at=query.period_at,
            assumed_roles=query.assumed_roles,
        )

    def review(
        self,
        actor: ActorContext,
        query: PolicyHistoryReviewInput,
        implementation_ref: str,
    ) -> PolicyHistoryReviewView:
        if query.expected_implementation_ref != implementation_ref:
            raise DomainError("CONFLICT", "Historical review implementation reference changed", 409)
        result = self.assessments.review(actor, self._query(actor, query))
        return PolicyHistoryReviewView.of(result, implementation_ref)

    def record_request(
        self, actor: ActorContext, value: PolicyHistoryRecordInput
    ) -> RecordHistoricalPolicyReview:
        return RecordHistoricalPolicyReview(
            query=self._query(actor, value),
            expected_implementation_ref=value.expected_implementation_ref,
            expected_context_fingerprint=value.expected_context_fingerprint,
            reason=value.reason,
        )

    def assess(
        self, actor: ActorContext, query: PolicyHistoryAssessmentInput
    ) -> PolicyHistoryAssessmentView:
        result = self.assessments.assess(actor, self._query(actor, query))
        return PolicyHistoryAssessmentView.of(result)
