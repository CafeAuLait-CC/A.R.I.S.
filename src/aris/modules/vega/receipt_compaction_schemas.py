from uuid import UUID

from pydantic import Field, model_validator

from aris.core.contracts import Contract
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery


class ReceiptCompactionRequest(Contract):
    id: UUID
    source: UsageArchiveHeadQuery
    first_seq: int = Field(ge=1)
    through_seq: int = Field(ge=1)
    block_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def bounded(self) -> "ReceiptCompactionRequest":
        if not 1 <= self.through_seq - self.first_seq + 1 <= 128:
            raise ValueError("Receipt compaction requires 1 to 128 consecutive acknowledgments")
        return self


class ReceiptCompactionResult(Contract):
    request: ReceiptCompactionRequest
    compacted_receipts: int
    duplicate: bool
