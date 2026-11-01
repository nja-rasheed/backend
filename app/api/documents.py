import json
import uuid
import os
import re
from typing import List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth_utils import get_current_user
from app.models.user import User
from app.models.service import GovernmentService
from app.models.document import CitizenDocument
from app.schemas.document import (
    DocumentResponse,
    DocumentReadinessCheckResponse
)
from app.services.document_verifier import semantic_verifier
from app.services.supabase_storage import supabase_storage

router = APIRouter(prefix="/api/documents", tags=["Citizen Document Vault"])
ALLOWED_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png"}
MAX_DOCUMENT_SIZE_BYTES = 5 * 1024 * 1024


def _has_valid_signature(content_type: str, file_bytes: bytes) -> bool:
    signatures = {
        "application/pdf": file_bytes.startswith(b"%PDF-"),
        "image/jpeg": file_bytes.startswith(b"\xff\xd8\xff"),
        "image/png": file_bytes.startswith(b"\x89PNG\r\n\x1a\n"),
    }
    return signatures.get(content_type, False)


@router.post(
    "/upload",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload citizen document to vault"
)
async def upload_document(
    file: UploadFile = File(...),
    document_type: str = Form(..., description="e.g. Identity Proof, Driving License, Income Certificate"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No file selected.")

    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF, JPEG, and PNG documents are supported."
        )
    if not _has_valid_signature(file.content_type, file_bytes):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file content does not match its declared type."
        )
    if len(file_bytes) > MAX_DOCUMENT_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Document exceeds the 5 MB size limit."
        )

    # Unique path inside the user's isolated folder in the private bucket
    unique_suffix = uuid.uuid4().hex[:8]
    sanitized_filename = re.sub(
        r"[^A-Za-z0-9._-]",
        "_",
        os.path.basename(file.filename)
    )
    storage_path = f"users/{current_user.id}/{unique_suffix}_{sanitized_filename}"

    # Stream to Supabase Storage
    try:
        supabase_storage.upload_file(
            destination_path=storage_path,
            file_bytes=file_bytes,
            content_type=file.content_type or "application/octet-stream"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Storage upload failed: {str(e)}"
        )

    # Persist document metadata in PostgreSQL
    new_doc = CitizenDocument(
        user_id=current_user.id,
        document_type=document_type,
        original_filename=file.filename,
        storage_path=storage_path,
        mime_type=file.content_type or "application/octet-stream",
        file_size_bytes=len(file_bytes)
    )
    db.add(new_doc)
    db.commit()
    db.refresh(new_doc)

    return new_doc


@router.get(
    "/",
    response_model=List[DocumentResponse],
    summary="List all uploaded documents for current citizen"
)
def list_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    user_docs = db.query(CitizenDocument).filter(CitizenDocument.user_id == current_user.id).all()

    # Attach active signed download URLs
    for doc in user_docs:
        doc.signed_url = (
            supabase_storage.get_signed_url(doc.storage_path, 3600)
            if doc.storage_path else None
        )

    return user_docs


@router.get("/readiness/{service_id}", response_model=DocumentReadinessCheckResponse)
async def check_document_readiness(
    service_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = db.query(GovernmentService).filter(GovernmentService.id == service_id).first()
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found.")

    try:
        required_docs_raw = (
            json.loads(service.required_documents)
            if isinstance(service.required_documents, str)
            else (service.required_documents or [])
        )
    except (TypeError, ValueError):
        required_docs_raw = []

    user_docs = db.query(CitizenDocument).filter(CitizenDocument.user_id == current_user.id).all()

    # Autonomous semantic matching
    semantic_result = await semantic_verifier.match_readiness(
        required_docs=required_docs_raw,
        uploaded_docs=user_docs
    )

    satisfied = []
    missing = []

    for item in semantic_result.matches:
        if item.matched_uploaded_id:
            satisfied.append(f"{item.required_document} (Matched: {item.matched_filename})")
        else:
            missing.append(item.required_document)

    total_count = len(required_docs_raw)
    sat_count = len(satisfied)
    percentage = round((sat_count / total_count * 100), 1) if total_count > 0 else 100.0

    return DocumentReadinessCheckResponse(
        service_id=service.id,
        service_name=service.name,
        total_required_count=total_count,
        available_count=sat_count,
        readiness_percentage=percentage,
        satisfied_documents=satisfied,
        missing_documents=missing,
        is_ready_for_execution=(len(missing) == 0)
    )