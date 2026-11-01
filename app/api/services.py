import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.service import GovernmentService, UserDiscoveredService
from app.models.user import User
from app.auth_utils import get_current_user
from app.schemas.service import GovernmentServiceResponse

router = APIRouter(prefix="/api/services", tags=["Government Services Discovery"])


def _format_service_record(service: GovernmentService) -> dict:
    return {
        "id": service.id,
        "service_code": service.service_code,
        "name": service.name,
        "category": service.category,
        "portal_url": service.portal_url,
        "description": service.description,
        "eligibility_rules": json.loads(service.eligibility_rules),
        "required_documents": json.loads(service.required_documents),
    }


@router.get("/", response_model=List[GovernmentServiceResponse])
def list_services(
    category: Optional[str] = Query(None, description="Filter by service category: education, grievance"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = (
        db.query(GovernmentService)
        .join(
            UserDiscoveredService,
            UserDiscoveredService.service_id == GovernmentService.id
        )
        .filter(UserDiscoveredService.user_id == current_user.id)
    )
    if category:
        query = query.filter(GovernmentService.category.ilike(f"%{category}%"))

    services = query.all()
    return [_format_service_record(s) for s in services]


@router.get("/my-services", response_model=List[GovernmentServiceResponse])
def list_my_discovered_services(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    user_services = (
        db.query(GovernmentService)
        .join(
            UserDiscoveredService,
            UserDiscoveredService.service_id == GovernmentService.id
        )
        .filter(UserDiscoveredService.user_id == current_user.id)
        .order_by(UserDiscoveredService.discovered_at.desc())
        .all()
    )
    unique_services = {}
    for service in user_services:
        unique_services.setdefault(service.id, service)
    return [_format_service_record(service) for service in unique_services.values()]


@router.delete(
    "/my-services/{service_id}",
    status_code=status.HTTP_200_OK,
    summary="Remove a discovered scheme from user dashboard"
)
def remove_user_discovered_service(
    service_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    association = db.query(UserDiscoveredService).filter(
        UserDiscoveredService.user_id == current_user.id,
        UserDiscoveredService.service_id == service_id
    ).first()
    if not association:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Scheme not found in your saved/discovered history."
        )

    db.delete(association)
    db.commit()
    return {"status": "success", "message": "Scheme removed from your dashboard."}


@router.get("/{service_id}", response_model=GovernmentServiceResponse)
def get_service_details(service_id: int, db: Session = Depends(get_db)):
    service = db.query(GovernmentService).filter(GovernmentService.id == service_id).first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service with ID {service_id} not found."
        )
    return _format_service_record(service)