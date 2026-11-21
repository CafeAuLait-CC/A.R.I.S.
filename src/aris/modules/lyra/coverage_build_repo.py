"""Lyra-owned immutable plans and keyset-free point progress, no second job queue."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.coverage_build_schemas import (
    MAX_BUILD_PLAN_BYTES,
    MAX_BUILD_STEP_BYTES,
    StoredCoverageBuild,
    StoredCoverageBuildStep,
)
from aris.modules.lyra.models import CoverageBuildPlanRow, CoverageBuildStepRow
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import canonical_trace
from aris.modules.lyra.window_schemas import MAX_WINDOW_MANIFESTS


def inconsistent_build() -> DomainError:
    return DomainError("DEPENDENCY_UNAVAILABLE", "Stored coverage build is inconsistent", 503)


class CoverageBuildRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, id: UUID) -> None:
        UsageTraceRepository(self.session).lock("coverage_build", str(id))

    def get(self, id: UUID) -> StoredCoverageBuild | None:
        row = (
            self.session.execute(
                select(*CoverageBuildPlanRow.__table__.c).where(CoverageBuildPlanRow.id == str(id))
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_plan"]
            if len(raw.encode()) > MAX_BUILD_PLAN_BYTES:
                raise ValueError("oversized coverage plan")
            result = StoredCoverageBuild.model_validate_json(raw)
            if canonical_trace(result) != raw or (
                str(result.request.id),
                str(result.event_id),
                result.configuration.trace.consumer_id,
                result.configuration.trace.node_id,
                result.configuration.fingerprint,
                result.fingerprint,
                result.created_at,
            ) != (
                row["id"],
                row["event_id"],
                row["consumer_id"],
                row["node_id"],
                row["configuration_fingerprint"],
                row["fingerprint"],
                utc(row["created_at"]),
            ):
                raise ValueError("coverage plan metadata differs")
            return result
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_build() from None

    def step(self, id: UUID, revision: int) -> StoredCoverageBuildStep | None:
        row = (
            self.session.execute(
                select(*CoverageBuildStepRow.__table__.c).where(
                    CoverageBuildStepRow.plan_id == str(id),
                    CoverageBuildStepRow.revision == revision,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_step"]
            if len(raw.encode()) > MAX_BUILD_STEP_BYTES:
                raise ValueError("oversized coverage step")
            result = StoredCoverageBuildStep.model_validate_json(raw)
            if canonical_trace(result) != raw or (
                str(result.plan_id),
                result.revision,
                str(result.attestation_id),
                str(result.manifest.id),
                str(result.event_id),
                result.fingerprint,
                result.created_at,
            ) != (
                row["plan_id"],
                row["revision"],
                row["attestation_id"],
                row["manifest_id"],
                row["event_id"],
                row["fingerprint"],
                utc(row["created_at"]),
            ):
                raise ValueError("coverage step metadata differs")
            return result
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_build() from None

    def latest(self, id: UUID) -> StoredCoverageBuildStep | None:
        revision = self.session.scalar(
            select(CoverageBuildStepRow.revision)
            .where(CoverageBuildStepRow.plan_id == str(id))
            .order_by(CoverageBuildStepRow.revision.desc())
            .limit(1)
        )
        return self.step(id, revision) if revision is not None else None

    def completed_steps(self, plan: StoredCoverageBuild) -> tuple[StoredCoverageBuildStep, ...]:
        # Keys only before proof bodies; the caller then applies the original manifest byte budget.
        revisions = tuple(
            self.session.scalars(
                select(CoverageBuildStepRow.revision)
                .where(CoverageBuildStepRow.plan_id == str(plan.request.id))
                .order_by(CoverageBuildStepRow.revision)
                .limit(MAX_WINDOW_MANIFESTS + 1)
            )
        )
        if revisions != tuple(range(plan.start_revision + 1, plan.target_revision + 1)):
            raise DomainError(
                "COVERAGE_BUILD_INCOMPLETE", "Coverage source build is incomplete", 409
            )
        result = []
        previous = None
        after = (
            plan.request.prefix_checkpoint.last_seq
            if plan.request.prefix_checkpoint
            else plan.configuration.trace.initial_after_seq
        )
        for revision in revisions:
            step = self.step(plan.request.id, revision)
            if step is None or (
                step.plan_fingerprint != plan.fingerprint
                or step.previous_fingerprint != previous
                or step.first_seq != after + 1
                or step.manifest.id != plan.manifest_id(revision)
                or step.created_at < plan.created_at
            ):
                raise inconsistent_build()
            result.append(step)
            previous, after = step.fingerprint, step.last_seq
        if after != plan.source.highest_committed_seq or (
            plan.tail is not None and result and result[-1].attestation_id != plan.tail.request.id
        ):
            raise inconsistent_build()
        return tuple(result)

    def append(self, plan: StoredCoverageBuild) -> None:
        self.session.add(
            CoverageBuildPlanRow(
                id=str(plan.request.id),
                event_id=str(plan.event_id),
                consumer_id=plan.configuration.trace.consumer_id,
                node_id=plan.configuration.trace.node_id,
                configuration_fingerprint=plan.configuration.fingerprint,
                fingerprint=plan.fingerprint,
                canonical_plan=canonical_trace(plan),
                created_at=plan.created_at,
            )
        )

    def append_step(self, value: StoredCoverageBuildStep) -> None:
        self.session.add(
            CoverageBuildStepRow(
                plan_id=str(value.plan_id),
                revision=value.revision,
                attestation_id=str(value.attestation_id),
                manifest_id=str(value.manifest.id),
                event_id=str(value.event_id),
                fingerprint=value.fingerprint,
                canonical_step=canonical_trace(value),
                created_at=value.created_at,
            )
        )
