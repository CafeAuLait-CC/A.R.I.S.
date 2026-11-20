"""Read-only preview for the exact feedback intent sent to Core confirmation."""

from typing import Literal

from pydantic import Field

from aris.core.access.schemas import AccessContract
from aris.modules.nova.policy_feedback_schemas import RecordPolicyFeedback


class PolicyFeedbackValidation(AccessContract):
    feedback_only: Literal[True] = True
    approval_performed: Literal[False] = False
    publication_performed: Literal[False] = False
    request: RecordPolicyFeedback = Field(repr=False)
    inline_bytes: int = Field(ge=1, le=12288)
