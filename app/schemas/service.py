from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class ServiceFilterQuery(BaseModel):
    category: Optional[str] = Field(None, description="e.g., education, grievance, civic")
    state: Optional[str] = Field(None, description="e.g., Tamil Nadu, All India")


class GovernmentServiceResponse(BaseModel):
    id: int
    service_code: str
    name: str
    category: str
    portal_url: str
    description: str
    eligibility_rules: Dict[str, Any]
    required_documents: List[str]

    class Config:
        from_attributes = True