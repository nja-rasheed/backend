from typing import List
from pydantic import BaseModel, Field


class AIUnderstandRequest(BaseModel):
    query: str = Field(
        default="",
        min_length=0,
        description="Natural language request or statement from the citizen.",
        example="I am an undergraduate student looking for government scholarships."
    )


class ServiceIntentResponse(BaseModel):
    service_category: str = Field(
        ...,
        description="Broad category of service (e.g., education, grievance, transport, welfare, unknown)"
    )
    service_name: str = Field(
        ...,
        description="Specific service name identified (e.g., scholarship, road_complaint, driving_licence)"
    )
    user_type: str = Field(
        ...,
        description="Identified user demographic role (e.g., student, citizen, business_owner)"
    )
    required_action: str = Field(
        ...,
        description="The immediate next action required (e.g., check_eligibility, file_complaint, review_documents)"
    )
    missing_information: List[str] = Field(
        default_factory=list,
        description="Information missing from the query needed to proceed"
    )
    summary: str = Field(
        ...,
        description="A concise, neutral summary of the user's intent."
    )