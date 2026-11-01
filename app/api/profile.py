from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.profile import CitizenProfile
from app.models.user import User
from app.schemas.profile import CitizenProfileCreate, CitizenProfileUpdate, CitizenProfileResponse
from app.auth_utils import get_current_user

router = APIRouter(prefix="/api/profile", tags=["Citizen Profile"])


@router.post("/", response_model=CitizenProfileResponse, status_code=status.HTTP_201_CREATED)
def create_profile(
    profile_data: CitizenProfileCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = db.query(CitizenProfile).filter(CitizenProfile.user_id == current_user.id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Profile already exists for this user. Use PUT to update."
        )

    profile = CitizenProfile(
        user_id=current_user.id,
        **profile_data.model_dump(exclude={"nationality"}),
        nationality="Indian"
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.get("/{user_id}", response_model=CitizenProfileResponse)
def get_profile(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access another citizen's profile"
        )
    profile = db.query(CitizenProfile).filter(CitizenProfile.user_id == user_id).first()
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")
    return profile


@router.put("/{user_id}", response_model=CitizenProfileResponse)
def update_profile(
    user_id: int,
    profile_data: CitizenProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to modify another citizen's profile"
        )
    profile = db.query(CitizenProfile).filter(CitizenProfile.user_id == user_id).first()
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    update_dict = profile_data.model_dump(exclude_unset=True)
    for key, value in update_dict.items():
        setattr(profile, key, value)

    db.commit()
    db.refresh(profile)
    return profile