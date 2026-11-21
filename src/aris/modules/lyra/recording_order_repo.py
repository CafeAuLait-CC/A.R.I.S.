"""Commit-serialized receipt membership, independent of unchanged physical ledger heads."""

from typing import Literal
from uuid import UUID

from sqlalchemy import SQLColumnExpression, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import Exists

from aris.modules.lyra.models import RecordingReceiptOrderRow


class RecordingReceiptOrderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def head(self, node_id: str) -> int:
        value = self.session.scalar(
            select(RecordingReceiptOrderRow.revision)
            .where(RecordingReceiptOrderRow.node_id == node_id)
            .order_by(RecordingReceiptOrderRow.revision.desc())
            .limit(1)
        )
        return value if value is not None else 0

    def append(self, kind: Literal["closed", "stable"], id: UUID, node_id: str) -> None:
        # Same node lock as the ledger, held until transaction commit; no timestamp watermark.
        from aris.modules.lyra.ledger_repo import UsageLedgerRepository

        UsageLedgerRepository(self.session).lock("node", node_id)
        self.session.add(
            RecordingReceiptOrderRow(
                kind=kind,
                receipt_id=str(id),
                node_id=node_id,
                revision=self.head(node_id) + 1,
            )
        )
        self.session.flush()

    @staticmethod
    def membership(
        kind: Literal["closed", "stable"],
        id_column: SQLColumnExpression[str],
        node_id: str,
        revision: int,
    ) -> Exists:
        return (
            select(RecordingReceiptOrderRow.receipt_id)
            .where(
                RecordingReceiptOrderRow.kind == kind,
                RecordingReceiptOrderRow.receipt_id == id_column,
                RecordingReceiptOrderRow.node_id == node_id,
                RecordingReceiptOrderRow.revision <= revision,
            )
            .exists()
        )
