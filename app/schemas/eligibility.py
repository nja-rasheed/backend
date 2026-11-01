from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel


class EligibilityStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    POTENTIALLY_ELIGIBLE = "POTENTIALLY_ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"


class CriterionStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    REQUIRES_VERIFICATION = "REQUIRES_VERIFICATION"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class CriterionEvaluation(BaseModel):
    criterion: str
    required: Any
    actual: Any
    passed: bool
    remarks: str
    status: CriterionStatus = CriterionStatus.PASSED
    source_url: Optional[str] = None


class EligibilityCheckResponse(BaseModel):
    service_id: int
    service_name: str
    service_code: str
    is_eligible: bool
    eligibility_status: EligibilityStatus = EligibilityStatus.INELIGIBLE
    evaluation_breakdown: List[CriterionEvaluation]
    required_documents_to_prepare: List[str]
    missing_profile_data: List[str]
    next_action_instructions: str