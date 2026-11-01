from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth_utils import get_current_user
from app.models.user import User
from app.models.profile import CitizenProfile
from app.models.service import GovernmentService
from app.schemas.eligibility import EligibilityCheckResponse
from app.services.eligibility_service import eligibility_service

router = APIRouter(prefix="/api/eligibility", tags=["Eligibility Checker"])


@router.get("/check/{service_id}", response_model=EligibilityCheckResponse)
def check_scheme_eligibility(
    service_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Evaluates citizen's profile against a discovered/saved government service.
    """
    service = db.query(GovernmentService).filter(GovernmentService.id == service_id).first()
    if not service:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service with id {service_id} not found."
        )

    profile = db.query(CitizenProfile).filter(CitizenProfile.user_id == current_user.id).first()

    return eligibility_service.evaluate(profile=profile, service=service)