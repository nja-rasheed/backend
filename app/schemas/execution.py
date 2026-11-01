from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: int
    citizen_approved: bool = False


class InterventionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intervention_type: str
    value: str = ""
    confirmed: bool = False


class WorkerCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    progress_percent: int = Field(default=0, ge=0, le=100)
    blocker: Optional[str] = None
    error: Optional[str] = None
    tracking_number: Optional[str] = None
    submission_evidence: Optional[str] = None


class ExecutionTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    execution_id: int
    service_id: int
    state: str
    progress_percent: int
    ready_for_dispatch: bool
    citizen_approved: bool
    target_portal_url: str
    blocker: Optional[str] = None
    tracking_number: Optional[str] = None
    last_error: Optional[str] = None
    updated_at: Optional[datetime] = None


class ExecutionDispatchResponse(ExecutionTaskResponse):
    pass


class ExecutionManifestData(BaseModel):
    model_config = ConfigDict(extra="allow")

    execution_id: int
    manifest: Dict[str, Any] = Field(default_factory=dict)
    form_payload: Dict[str, Any] = Field(default_factory=dict)
    ready_for_dispatch: bool
