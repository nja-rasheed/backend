from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class DocumentResponse(BaseModel):
    id: int
    document_type: str
    original_filename: str
    mime_type: str
    file_size_bytes: int
    storage_path: str
    signed_url: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# Alias so both imports work cleanly across services
CitizenDocumentResponse = DocumentResponse


class DocumentReadinessCheckResponse(BaseModel):
    service_id: int
    service_name: str
    total_required_count: int
    available_count: int
    readiness_percentage: float
    satisfied_documents: List[str]
    missing_documents: List[str]
    is_ready_for_execution: bool


class DocumentRequirementStatus(BaseModel):
    requirement: str
    classification: str = "MANDATORY"
    status: str
    filename: Optional[str] = None


class DocumentMatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_document: str = Field(description="The exact required document name from the scheme")
    matched_uploaded_id: int = Field(
        default=0,
        description="The ID of the user's document that satisfies this requirement, or None if missing"
    )
    matched_filename: str = Field(default="", description="Filename of the matched document")
    confidence_reasoning: str = Field(description="Brief explanation of why this document matches or why it is missing")


class SemanticReadinessResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matches: List[DocumentMatchItem] = Field(default_factory=list)