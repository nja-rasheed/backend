
import json
import logging
import re
from datetime import date
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth_utils import get_current_user
from app.models.user import User
from app.models.profile import CitizenProfile
from app.models.service import GovernmentService
from app.models.document import CitizenDocument
from app.models.execution import ExecutionTask
from app.services.supabase_storage import supabase_storage
from app.services.document_verifier import semantic_verifier
from app.schemas.document import DocumentRequirementStatus
from app.services.application_state import ApplicationState
from app.schemas.execution import (
    DispatchRequest,
    ExecutionTaskResponse,
    InterventionRequest,
    WorkerCallbackRequest,
)

logger = logging.getLogger("agent.execution")
router = APIRouter(prefix="/api/execution", tags=["Bureaucracy Execution Engine"])


class ExecutionDraftRequest(BaseModel):
    service_id: int = Field(description="Database ID of the target GovernmentService")
    citizen_query: Optional[str] = Field(
        default="",
        description="Optional grievance/complaint text if filing a narrative request"
    )
    additional_fields: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Transaction-specific fields (e.g., license_number, chassis_number, landmark)"
    )


class MatchedAttachment(BaseModel):
    requirement: str
    filename: str
    download_url: str


class ExecutionManifestResponse(BaseModel):
    execution_id: Optional[int] = None
    service_id: int
    service_name: str
    service_code: str
    portal_url: str
    governance_tier: str
    ready_for_dispatch: bool
    missing_form_fields: List[str] = Field(default_factory=list)
    missing_documents: List[str] = Field(default_factory=list)
    field_statuses: List[Dict[str, str]] = Field(default_factory=list)
    document_statuses: List[DocumentRequirementStatus] = Field(default_factory=list)
    workflow_state: str = "DRAFT"
    form_payload: Dict[str, Any] = Field(default_factory=dict)
    file_attachments: List[MatchedAttachment] = Field(default_factory=list)


def _load_json_collection(value: Any) -> List[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            logger.warning("[ExecutionDraft] Ignoring malformed stored requirement JSON.")
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _requirement_names(raw_requirements: Any) -> tuple[List[str], Dict[str, str]]:
    names: List[str] = []
    classifications: Dict[str, str] = {}
    for requirement in raw_requirements or []:
        if isinstance(requirement, dict):
            name = str(
                requirement.get("name")
                or requirement.get("field")
                or requirement.get("document")
                or ""
            ).strip()
            classification = str(
                requirement.get("classification")
                or requirement.get("status")
                or "MANDATORY"
            ).upper()
        else:
            name = str(requirement).strip()
            classification = "MANDATORY"
        if name:
            names.append(name)
            classifications[name] = classification
    return names, classifications


def _mask_sensitive_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    masked = dict(payload)
    for key, value in masked.items():
        normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
        if any(token in normalized_key for token in ("aadhaar", "aadhar", "bankaccount", "accountnumber")):
            text = str(value)
            masked[key] = f"XXXX-XXXX-{text[-4:]}" if len(text) >= 4 else "XXXX"
    return masked


def _calculate_age(profile: Optional[CitizenProfile]) -> Optional[int]:
    """Calculate current age from DOB, falling back to stored age."""

    if not profile:
        return None

    if profile.dob:
        try:
            dob = date.fromisoformat(str(profile.dob).strip())
            today = date.today()

            age = today.year - dob.year

            if (today.month, today.day) < (dob.month, dob.day):
                age -= 1

            return age

        except (ValueError, TypeError):
            logger.warning("[ExecutionDraft] Invalid DOB format in citizen profile.")

    return profile.age


@router.post(
    "/draft",
    response_model=ExecutionManifestResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate Pre-Validated Execution Manifest for Playwright"
)
async def generate_execution_draft(
    request: ExecutionDraftRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # 1. Fetch Target Government Service
    service = db.query(GovernmentService).filter(
        GovernmentService.id == request.service_id
    ).first()

    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Government service with ID {request.service_id} not found."
        )

    # 2. Fetch User Profile & Document Vault
    profile = db.query(CitizenProfile).filter(
        CitizenProfile.user_id == current_user.id
    ).first()

    user_docs = db.query(CitizenDocument).filter(
        CitizenDocument.user_id == current_user.id
    ).all()

    # 3. Parse Stored Requirements
    raw_docs = getattr(service, "required_documents", "[]")
    raw_required_docs: List[Any] = _load_json_collection(raw_docs)
    required_docs, document_classifications = _requirement_names(raw_required_docs)

    raw_fields = getattr(service, "required_form_fields", "[]")
    raw_required_fields: List[Any] = _load_json_collection(raw_fields)
    required_fields, field_classifications = _requirement_names(raw_required_fields)

    # 4. Semantic Document Verification & Signed URL Generation
    missing_docs: List[str] = []
    matched_attachments: List[MatchedAttachment] = []
    document_statuses: List[DocumentRequirementStatus] = []

    if required_docs:
        try:
            verification_result = await semantic_verifier.match_readiness(
                required_docs=required_docs,
                uploaded_docs=user_docs
            )
        except Exception as e:
            logger.error(f"[ExecutionDraft] Semantic verifier failed: {str(e)}")
            verification_result = None

        if verification_result and verification_result.matches:
            for match in verification_result.matches:
                if match.matched_uploaded_id:
                    doc_obj = next(
                        (d for d in user_docs if d.id == match.matched_uploaded_id),
                        None
                    )

                    if doc_obj and doc_obj.storage_path and doc_obj.original_filename:
                        signed_url = supabase_storage.get_signed_url(
                            doc_obj.storage_path, 3600
                        )

                        matched_attachments.append(
                            MatchedAttachment(
                                requirement=match.required_document,
                                filename=doc_obj.original_filename,
                                download_url=signed_url or ""
                            )
                        )
                        document_statuses.append(DocumentRequirementStatus(
                            requirement=match.required_document,
                            classification=document_classifications.get(
                                match.required_document, "MANDATORY"
                            ),
                            status="AVAILABLE",
                            filename=doc_obj.original_filename
                        ))
                    elif doc_obj:
                        document_statuses.append(DocumentRequirementStatus(
                            requirement=match.required_document,
                            classification=document_classifications.get(
                                match.required_document, "MANDATORY"
                            ),
                            status="REQUIRES_VERIFICATION",
                            filename=doc_obj.original_filename or None
                        ))
                else:
                    classification = document_classifications.get(
                        match.required_document, "MANDATORY"
                    )
                    if classification in {"MANDATORY", "CONDITIONALLY_REQUIRED"}:
                        missing_docs.append(match.required_document)
                    document_statuses.append(DocumentRequirementStatus(
                        requirement=match.required_document,
                        classification=classification,
                        status="MISSING"
                    ))

        else:
            missing_docs = [
                requirement for requirement in required_docs
                if document_classifications.get(requirement, "MANDATORY")
                in {"MANDATORY", "CONDITIONALLY_REQUIRED"}
            ]
            document_statuses = [
                DocumentRequirementStatus(
                    requirement=requirement,
                    classification=document_classifications.get(requirement, "MANDATORY"),
                    status="REQUIRES_VERIFICATION"
                )
                for requirement in required_docs
            ]

    # 5. Build Combined Pool with All Baseline Profile Attributes
    dob_val = str(profile.dob).strip() if (profile and profile.dob) else ""
    phone_val = str(profile.phone_number).strip() if (profile and profile.phone_number) else ""
    age_val = _calculate_age(profile)
    income_val = (
        str(profile.annual_family_income)
        if (profile and profile.annual_family_income is not None)
        else ""
    )
    edu_val = (
        profile.education_level
        if (profile and profile.education_level)
        else ""
    )

    combined_pool: Dict[str, Any] = {
        "full_name": profile.full_name if (profile and profile.full_name) else current_user.username,
        "email": current_user.email,
        "dob": dob_val,
        "date_of_birth": dob_val,
        "birth_date": dob_val,
        "age": age_val if age_val is not None else "",
        "applicant_age": age_val if age_val is not None else "",
        "gender": profile.gender if (profile and profile.gender) else "",
        "applicant_gender": profile.gender if (profile and profile.gender) else "",
        "state": profile.state if (profile and profile.state) else "",
        "district": profile.district if (profile and profile.district) else "",
        "phone_number": phone_val,
        "mobile_number": phone_val,
        "annual_family_income": income_val,
        "family_income": income_val,
        "annual_income": income_val,
        "income": income_val,
        "occupation_type": profile.occupation_type if profile else "",
        "nationality": getattr(profile, "nationality", "") if profile else "",
    }

    if profile:
        occ = (profile.occupation_type or "").lower()

        if occ == "student":
            combined_pool["education_level"] = edu_val
            combined_pool["educational_qualification"] = edu_val
            combined_pool["qualification"] = edu_val
            combined_pool["current_education"] = edu_val
            combined_pool["institution_name"] = profile.institution_name or ""
            combined_pool["college_name"] = profile.institution_name or ""
            combined_pool["school_name"] = profile.institution_name or ""
            combined_pool["course_or_degree"] = profile.course_or_degree or ""

        elif occ == "worker":
            combined_pool["employment_type"] = profile.employment_type or ""
            combined_pool["industry_or_sector"] = profile.industry_or_sector or ""
            combined_pool["employer_name"] = profile.employer_name or ""
            combined_pool["company_name"] = profile.employer_name or ""

    if request.citizen_query:
        combined_pool["grievance_description"] = request.citizen_query

    # Merge dynamic additional fields provided by frontend
    if request.additional_fields:
        for k, v in request.additional_fields.items():
            if v is not None and str(v).strip() != "":
                normalized_key = re.sub(r"[^a-z0-9]", "", k.lower())
                combined_pool[normalized_key] = v

    # 6. Validate Required Form Fields Against Combined Pool with Semantic Aliasing
    missing_fields: List[str] = []
    final_form_payload: Dict[str, Any] = {}
    field_statuses: List[Dict[str, str]] = []
    pool_normalized = {
        re.sub(r"[^a-z0-9]", "", key.lower()): value
        for key, value in combined_pool.items()
    }

    income_val = next(
        (
            value for key, value in pool_normalized.items()
            if key in {"annualfamilyincome", "familyincome", "annualincome", "income"}
            and value is not None
            and str(value).strip() != ""
        ),
        ""
    )
    edu_val = next(
        (
            value for key, value in pool_normalized.items()
            if key in {
                "educationlevel",
                "educationalqualification",
                "qualification",
                "currenteducation"
            }
            and value is not None
            and str(value).strip() != ""
        ),
        ""
    )

    for field_name in required_fields:
        norm_key = re.sub(r"[^a-z0-9]", "", field_name.lower())
        value = pool_normalized.get(norm_key)

        # Synonym resolving
        if not value:
            if "income" in norm_key or "familyincome" in norm_key:
                value = income_val
            elif (
                "education" in norm_key
                or "qualification" in norm_key
                or "academic" in norm_key
            ):
                value = edu_val

            # Name synonyms
            elif norm_key in ["name", "applicantname", "candidatename", "ownername", "citizenname"]:
                value = combined_pool.get("full_name")

            # Date of Birth synonyms
            elif norm_key in ["dob", "dateofbirth", "birthdate", "applicantdob"]:
                value = combined_pool.get("dob") or combined_pool.get("date_of_birth")

            # Age synonyms
            elif norm_key in ["applicantage", "ageyears", "candidateage"]:
                value = combined_pool.get("age")

            # Gender synonyms
            elif norm_key in ["applicantgender", "sex", "applicantsex"]:
                value = combined_pool.get("gender")

            # Phone / Mobile synonyms
            elif norm_key in ["contactnumber", "phone", "mobile", "mobileno", "cellnumber", "phoneno"]:
                value = combined_pool.get("phone_number") or combined_pool.get("mobile_number")

            # Query / Description synonyms
            elif norm_key in ["complaint", "description", "details", "reason", "grievance", "remarks"]:
                value = combined_pool.get("grievance_description")

            # Regional location synonyms
            elif norm_key in ["city", "town", "taluk"]:
                value = combined_pool.get("district")

            elif norm_key in ["statename"]:
                value = combined_pool.get("state")

            # Education synonyms
            elif norm_key in ["college", "institution", "school", "university"]:
                value = combined_pool.get("institution_name")

            elif norm_key in ["degree", "course"]:
                value = combined_pool.get("course_or_degree") or combined_pool.get("education_level")

            # Work synonyms
            elif norm_key in ["employer", "company", "organization"]:
                value = combined_pool.get("employer_name")

        if value is not None and str(value).strip() != "":
            final_form_payload[norm_key] = value
        else:
            classification = field_classifications.get(field_name, "MANDATORY")
            if classification in {"MANDATORY", "CONDITIONALLY_REQUIRED"}:
                missing_fields.append(field_name)
            field_statuses.append({
                "field": field_name,
                "classification": classification,
                "status": "MISSING"
            })
            continue

        field_statuses.append({
            "field": field_name,
            "classification": field_classifications.get(field_name, "MANDATORY"),
            "status": "AVAILABLE"
        })

    # Retain all core identity & demographic attributes in final_form_payload
    core_keys = [
        "full_name",
        "email",
        "state",
        "district",
        "mobile_number",
        "phone_number",
        "date_of_birth",
        "age",
        "gender",
        "nationality"
    ]

    for core_key in core_keys:
        if (
            core_key in combined_pool
            and core_key not in final_form_payload
            and combined_pool[core_key]
        ):
            final_form_payload[core_key] = combined_pool[core_key]

    if "grievance_description" in combined_pool and "grievance_description" not in final_form_payload:
        final_form_payload["grievance_description"] = combined_pool["grievance_description"]

    is_ready = (len(missing_docs) == 0 and len(missing_fields) == 0)

    manifest = ExecutionManifestResponse(
        service_id=service.id,
        service_name=service.name,
        service_code=service.service_code,
        portal_url=service.portal_url,
        governance_tier=getattr(service, "governance_tier", "state") or "state",
        ready_for_dispatch=is_ready,
        missing_form_fields=missing_fields,
        missing_documents=missing_docs,
        field_statuses=field_statuses,
        document_statuses=document_statuses,
        workflow_state=(
            ApplicationState.DRAFT.value
            if not is_ready
            else ApplicationState.DOCUMENT_VALIDATION.value
        ),
        form_payload=_mask_sensitive_payload(final_form_payload),
        file_attachments=matched_attachments
    )
    task = ExecutionTask(
        user_id=current_user.id,
        service_id=service.id,
        state=(
            ApplicationState.READY_FOR_DISPATCH.value
            if is_ready else ApplicationState.DRAFT.value
        ),
        ready_for_dispatch=is_ready,
        manifest_json=json.dumps(manifest.model_dump(exclude={"execution_id"})),
        form_payload_json=json.dumps(manifest.form_payload),
        progress_percent=0,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    manifest.execution_id = task.id
    return manifest


def _task_response(task: ExecutionTask, service: GovernmentService) -> ExecutionTaskResponse:
    return ExecutionTaskResponse(
        execution_id=task.id,
        service_id=task.service_id,
        state=task.state,
        progress_percent=task.progress_percent,
        ready_for_dispatch=bool(task.ready_for_dispatch),
        citizen_approved=bool(task.citizen_approved),
        target_portal_url=service.portal_url,
        blocker=task.blocker,
        tracking_number=task.tracking_number,
        last_error=task.last_error,
        updated_at=task.updated_at,
    )


def _owned_task(
    execution_id: int,
    current_user: User,
    db: Session,
) -> tuple[ExecutionTask, GovernmentService]:
    task = db.query(ExecutionTask).filter(
        ExecutionTask.id == execution_id,
        ExecutionTask.user_id == current_user.id,
    ).first()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution task not found."
        )
    service = db.query(GovernmentService).filter(
        GovernmentService.id == task.service_id
    ).first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Execution task references an unavailable service."
        )
    return task, service


@router.post(
    "/dispatch",
    response_model=ExecutionTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Dispatch an approved execution task"
)
def dispatch_execution(
    request: DispatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task, service = _owned_task(request.execution_id, current_user, db)
    if not task.ready_for_dispatch:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Execution task is not ready for dispatch."
        )
    if not request.citizen_approved:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="Explicit citizen approval is required before dispatch."
        )

    task.citizen_approved = True
    task.state = ApplicationState.DISPATCHED.value
    task.progress_percent = max(task.progress_percent, 1)
    task.blocker = None
    db.commit()
    db.refresh(task)
    return _task_response(task, service)


@router.get(
    "/status/{execution_id}",
    response_model=ExecutionTaskResponse,
    summary="Get execution task status"
)
def execution_status(
    execution_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task, service = _owned_task(execution_id, current_user, db)
    return _task_response(task, service)


@router.post(
    "/intervene/{execution_id}",
    response_model=ExecutionTaskResponse,
    summary="Resume an execution task after citizen intervention"
)
def intervene_execution(
    execution_id: int,
    request: InterventionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task, service = _owned_task(execution_id, current_user, db)
    if task.state != ApplicationState.WAITING_FOR_CITIZEN_INTERVENTION.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This execution task is not waiting for intervention."
        )
    if not request.confirmed:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="The intervention must be explicitly confirmed."
        )

    task.state = ApplicationState.DISPATCHED.value
    task.blocker = None
    db.commit()
    db.refresh(task)
    return _task_response(task, service)


@router.post(
    "/callback/{execution_id}",
    response_model=ExecutionTaskResponse,
    summary="Receive a downstream execution worker callback"
)
def execution_callback(
    execution_id: int,
    request: WorkerCallbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task, service = _owned_task(execution_id, current_user, db)
    try:
        requested_state = ApplicationState(request.state)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unsupported execution state."
        ) from exc

    task.state = requested_state.value
    task.progress_percent = request.progress_percent
    task.blocker = request.blocker
    task.last_error = request.error
    task.tracking_number = request.tracking_number or task.tracking_number
    task.submission_evidence = (
        request.submission_evidence or task.submission_evidence
    )
    db.commit()
    db.refresh(task)
    return _task_response(task, service)
