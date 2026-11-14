from datetime import datetime
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.interfaces.slack.models import SlackHomeJob, SlackReceipt


class SlackRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def receive(
        self, team_id: str, external_id: str, fingerprint: str, kind: str, now: datetime
    ) -> tuple[SlackReceipt, bool]:
        insert = sqlite_insert if self.session.get_bind().dialect.name == "sqlite" else pg_insert
        inserted = self.session.scalar(
            insert(SlackReceipt)
            .values(
                id=str(uuid4()),
                team_id=team_id,
                external_id=external_id,
                fingerprint=fingerprint,
                kind=kind,
                received_at=now,
            )
            .on_conflict_do_nothing()
            .returning(SlackReceipt.id)
        )
        receipt = self.session.scalars(
            select(SlackReceipt)
            .where(SlackReceipt.team_id == team_id, SlackReceipt.external_id == external_id)
            .with_for_update()
        ).one()
        return receipt, inserted is not None

    def add_home_job(self, receipt: SlackReceipt, user_id: str, now: datetime) -> None:
        self.session.add(
            SlackHomeJob(
                id=str(uuid4()),
                receipt_id=receipt.id,
                team_id=receipt.team_id,
                user_id=user_id,
                state="pending",
                attempts=0,
                available_at=now,
                created_at=now,
            )
        )

    def next_job(self, now: datetime) -> SlackHomeJob | None:
        return self.session.scalar(
            select(SlackHomeJob)
            .where(
                or_(
                    and_(SlackHomeJob.state == "pending", SlackHomeJob.available_at <= now),
                    and_(SlackHomeJob.state == "processing", SlackHomeJob.lease_until <= now),
                )
            )
            .order_by(SlackHomeJob.available_at, SlackHomeJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )

    def lock_job(self, job_id: str) -> SlackHomeJob | None:
        return self.session.scalar(
            select(SlackHomeJob).where(SlackHomeJob.id == job_id).with_for_update()
        )
