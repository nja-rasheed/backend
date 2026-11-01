import json
from typing import List, Optional
from sqlalchemy import func
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.service import GovernmentService, UserDiscoveredService
from app.models.user import User
from app.models.profile import CitizenProfile
from app.auth_utils import get_current_user
from app.services.discovery_service import discovery_service
from app.services.gemini_service import gemini_service
from app.schemas.agent_pipeline import DiscoveredSchemeItem
from app.schemas.service import GovernmentServiceResponse

router = APIRouter(prefix="/api/discovery", tags=["Dynamic Scheme Discovery"])


@router.get("/search", response_model=List[GovernmentServiceResponse])
async def search_schemes_live(
    query: str = Query(..., min_length=3, description="Citizen query e.g. college scholarship, road repair"),
    state: Optional[str] = Query(None, description="Optional state override. Defaults to profile state."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Direct endpoint to dynamically discover government schemes.
    """
    effective_state = state
    if not effective_state:
        profile = db.query(CitizenProfile).filter(CitizenProfile.user_id == current_user.id).first()
        effective_state = profile.state if profile else None

    intent = await gemini_service.understand_request(query)
    discovered: List[DiscoveredSchemeItem] = await discovery_service.discover_schemes(
        service_category=intent.service_category,
        service_name=intent.service_name,
        citizen_query=query,
        user_state=effective_state
    )
    unique_discovered = []
    seen_scheme_keys = set()
    for item in discovered:
        scheme_key = item.service_code.strip().lower() or item.portal_url.strip().lower()
        if scheme_key and scheme_key not in seen_scheme_keys:
            seen_scheme_keys.add(scheme_key)
            unique_discovered.append(item)
    discovered = unique_discovered

    saved_services = []
    for item in discovered:
        existing = db.query(GovernmentService).filter(
            GovernmentService.service_code == item.service_code
        ).first()

        rules_dict = item.eligibility_criteria.model_dump()

        if not existing:
            existing = GovernmentService(
                service_code=item.service_code,
                name=item.name,
                category=item.category,
                portal_url=item.portal_url,
                description=item.description,
                eligibility_rules=json.dumps(rules_dict),
                required_documents=json.dumps(item.required_documents),
            )
            db.add(existing)
            db.commit()
            db.refresh(existing)
        existing_link = db.query(UserDiscoveredService).filter(
            UserDiscoveredService.user_id == current_user.id,
            UserDiscoveredService.service_id == existing.id
        ).first()
        if not existing_link:
            db.add(UserDiscoveredService(
                user_id=current_user.id,
                service_id=existing.id
            ))
        else:
            existing_link.discovered_at = func.now()
        db.commit()

        saved_services.append(
            GovernmentServiceResponse(
                id=existing.id,
                service_code=existing.service_code,
                name=existing.name,
                category=existing.category,
                portal_url=existing.portal_url,
                description=existing.description,
                eligibility_rules=json.loads(existing.eligibility_rules) if isinstance(existing.eligibility_rules, str) else existing.eligibility_rules,
                required_documents=json.loads(existing.required_documents) if isinstance(existing.required_documents, str) else existing.required_documents,
            )
        )

    return saved_services