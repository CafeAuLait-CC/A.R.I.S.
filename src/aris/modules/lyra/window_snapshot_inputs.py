"""Read and reverify selected committed inputs within the caller's database snapshot."""

from datetime import datetime
from time import monotonic

from sqlalchemy.orm import Session

from aris.core.access.history_schemas import HistoricalNodeUserBatch, HistoricalNodeUserQuery
from aris.core.access.history_service import NodeUserHistoryService
from aris.core.contracts import DomainError
from aris.modules.lyra.coverage_build_schemas import StoredCoverageBuild, StoredCoverageBuildStep
from aris.modules.lyra.coverage_repo import MANIFEST_BATCH_SIZE, ProcessingCoverageRepository
from aris.modules.lyra.coverage_schemas import (
    CoverageIdentity,
    StoredProcessingCoverage,
    coverage_identity_key,
)
from aris.modules.lyra.coverage_service import ProcessingCoverageService
from aris.modules.lyra.residency_state import ResidencyRun
from aris.modules.lyra.window_snapshot_schemas import WindowManifestSelection


def selected_window_manifests(
    session: Session,
    coverage: ProcessingCoverageService,
    selections: tuple[WindowManifestSelection, ...],
    deadline: float,
) -> tuple[StoredProcessingCoverage, ...]:
    result: list[StoredProcessingCoverage] = []
    for selection in selections:
        value = coverage.reverify_in_transaction(session, selection.id)
        if value.fingerprint != selection.fingerprint:
            raise DomainError("CONFLICT", "Selected window manifest fingerprint differs", 409)
        result.append(value)
        if monotonic() >= deadline:
            raise DomainError(
                "RETRYABLE_DATABASE_ERROR", "Window source verification budget exceeded", 503
            )
    return tuple(result)


def committed_build_manifests(
    session: Session,
    plan: StoredCoverageBuild,
    steps: tuple[StoredCoverageBuildStep, ...],
    deadline: float,
) -> tuple[StoredProcessingCoverage, ...]:
    """Use the proofs committed by a completed build step under its fixed boundaries."""
    repository = ProcessingCoverageRepository(session)
    result = []
    for start in range(0, len(steps), MANIFEST_BATCH_SIZE):
        batch = steps[start : start + MANIFEST_BATCH_SIZE]
        manifests = repository.get_batch(tuple(step.manifest.id for step in batch))
        for step in batch:
            manifest = manifests.get(step.manifest.id)
            if manifest is None or manifest.fingerprint != step.manifest.fingerprint:
                raise DomainError("CONFLICT", "Committed build manifest differs", 409)
            analysis = manifest.analysis
            if (
                analysis.configuration != plan.configuration
                or analysis.read_at != plan.read_at
                or (analysis.identities and analysis.identity_revision != plan.identity_revision)
                or analysis.request.ledger != plan.request.ledger
                or analysis.request.start_at != plan.request.query.start_at
                or analysis.request.end_at != plan.request.query.end_at
                or analysis.request.attestation_id != step.attestation_id
                or analysis.attestation.fingerprint != step.attestation_fingerprint
                or analysis.attestation.revision != step.revision
                or analysis.attestation.first_seq != step.first_seq
                or analysis.attestation.last_seq != step.last_seq
            ):
                raise DomainError("CONFLICT", "Committed manifest source or window differs", 409)
            result.append(manifest)
        if monotonic() >= deadline:
            raise DomainError(
                "RETRYABLE_DATABASE_ERROR", "Build manifest read budget exceeded", 503
            )
    return tuple(result)


def window_identities(
    session: Session,
    identities: NodeUserHistoryService,
    manifests: tuple[StoredProcessingCoverage, ...],
    revision: int | None,
    read_at: datetime,
    deadline: float,
) -> tuple[CoverageIdentity, ...]:
    if revision is None:
        return ()
    parents: dict[tuple[str, str, int, str], ResidencyRun] = {}
    for manifest in manifests:
        for cell in manifest.analysis.cells:
            if cell.state != "recorded":
                continue
            assert cell.identity_index is not None
            parent = manifest.analysis.identities[cell.identity_index].run
            key = coverage_identity_key(parent)
            if key in parents and parents[key] != parent:
                raise DomainError("CONFLICT", "Window parent evidence differs", 409)
            parents[key] = parent
    ordered = [parents[key] for key in sorted(parents)]
    result: list[CoverageIdentity] = []
    for offset in range(0, len(ordered), 128):
        runs = ordered[offset : offset + 128]
        answers = identities.resolve_many_in_transaction(
            session,
            HistoricalNodeUserBatch(
                queries=tuple(
                    HistoricalNodeUserQuery(
                        node_id=run.first.node_id,
                        os_username=run.username,
                        start_at=run.first.compute_observed_at,
                        end_at=run.last.compute_observed_at,
                        as_of=read_at,
                        through_revision=revision,
                    )
                    for run in runs
                )
            ),
        )
        if answers.directory_revision != revision:
            raise DomainError("CONFLICT", "Window identity history boundary differs", 409)
        result.extend(
            CoverageIdentity(run=run, identity=answer)
            for run, answer in zip(runs, answers.answers, strict=True)
        )
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Window identity budget exceeded", 503)
    return tuple(result)
