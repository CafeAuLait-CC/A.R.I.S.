from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.contracts import Contract, DomainError
from aris.core.time import utc
from aris.interfaces.slack.client import HomePublisher, SlackDeliveryError
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.home import home_view
from aris.interfaces.slack.repo import SlackRepository
from aris.interfaces.slack.schemas import HomeJobClaim
from aris.modules.vega.service import VegaService


class DispatchResult(Contract):
    outcome: Literal["idle", "delivered", "retry", "failed", "lease_lost"]
    job_id: str | None = None


class SlackDispatcher:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        config: SlackSettings,
        vega: VegaService,
        publisher: HomePublisher,
    ) -> None:
        self.sessions = sessions
        self.config = config
        self.vega = vega
        self.publisher = publisher

    def claim(self) -> HomeJobClaim | DispatchResult | None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            job = SlackRepository(session).next_job(now)
            if job is None:
                return None
            if job.attempts >= self.config.job_max_attempts:
                job.state = "failed"
                job.last_error_code = "attempts_exhausted"
                job.lease_token = None
                job.lease_until = None
                session.add(
                    AuditEntry(
                        id=str(uuid4()),
                        actor="slack-worker",
                        action="slack.home.failed",
                        target=job.user_id,
                        request_id=job.id,
                        recorded_at=now,
                        changes={"attempts": job.attempts, "error_code": "attempts_exhausted"},
                    )
                )
                return DispatchResult(outcome="failed", job_id=job.id)
            job.state = "processing"
            job.attempts += 1
            job.lease_token = str(uuid4())
            job.lease_until = now + timedelta(seconds=self.config.job_lease_seconds)
            return HomeJobClaim(
                job.id, job.team_id, job.user_id, job.lease_token, job.attempts, job.lease_until
            )

    def finish(self, claim: HomeJobClaim, error: SlackDeliveryError | None) -> DispatchResult:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            job = SlackRepository(session).lock_job(claim.job_id)
            if (
                job is None
                or job.state != "processing"
                or job.lease_token != claim.lease_token
                or job.lease_until is None
                or utc(job.lease_until) <= now
            ):
                return DispatchResult(outcome="lease_lost", job_id=claim.job_id)
            job.lease_token = None
            job.lease_until = None
            outcome: Literal["delivered", "retry", "failed"]
            if error is None:
                job.state = "completed"
                job.completed_at = now
                job.last_error_code = None
                outcome = "delivered"
            else:
                job.last_error_code = error.code
                if not error.retryable or job.attempts >= self.config.job_max_attempts:
                    job.state = "failed"
                    outcome = "failed"
                else:
                    job.state = "pending"
                    delay = max(error.retry_after, min(300, 5 * 2 ** min(job.attempts - 1, 6)))
                    job.available_at = now + timedelta(seconds=delay)
                    outcome = "retry"
            session.add(
                AuditEntry(
                    id=str(uuid4()),
                    actor="slack-worker",
                    action="slack.home." + outcome,
                    target=job.user_id,
                    request_id=job.id,
                    recorded_at=now,
                    changes={"attempts": job.attempts, "error_code": job.last_error_code or ""},
                )
            )
        return DispatchResult(outcome=outcome, job_id=claim.job_id)

    def run_once(self) -> DispatchResult:
        if not self.config.configured:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Slack is not configured", 503)
        claim = self.claim()
        if claim is None:
            return DispatchResult(outcome="idle")
        if isinstance(claim, DispatchResult):
            return claim
        error: SlackDeliveryError | None = None
        try:
            if claim.team_id != self.config.team_id:
                raise SlackDeliveryError("workspace_mismatch", retryable=False)
            self.publisher.publish_home(
                claim.user_id,
                home_view(
                    self.vega.resource_status(),
                    asset_base_url=self.config.home_asset_base_url,
                    layout=self.config.home_layout,
                ),
            )
        except SlackDeliveryError as exc:
            error = exc
        except Exception:
            error = SlackDeliveryError("dependency_unavailable")
        return self.finish(claim, error)
