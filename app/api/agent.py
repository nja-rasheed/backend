import json
import time
import logging
import re
from typing import List, Dict, Any
from datetime import datetime
from sqlalchemy import func
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth_utils import get_current_user
from app.models.user import User
from app.models.profile import CitizenProfile
from app.models.service import GovernmentService, UserDiscoveredService
from app.services.gemini_service import gemini_service
from app.services.discovery_service import calculate_capability_rank, discovery_service
from app.services.eligibility_service import eligibility_service
from app.services.privacy import redact_sensitive_text
from app.schemas.ai import AIUnderstandRequest
from app.schemas.agent_pipeline import (
    AutonomousAgentAssistanceResponse,
    EvaluatedSchemeItem
)

logger = logging.getLogger("agent.pipeline")
router = APIRouter(prefix="/api/agent", tags=["Autonomous Citizen Agent"])


def _redact_query_for_log(value: str) -> str:
    redacted = re.sub(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "[Aadhaar Redacted]", value)
    return re.sub(
        r"\b\d{9,18}\b",
        "[Account Masked]",
        redacted,
    )


def _load_json_list(value: Any) -> List[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _find_cached_services(
    db: Session,
    query: str,
    service_category: str,
    service_name: str,
) -> List[GovernmentService]:
    def tokens(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", (value or "").lower())
            if len(token) > 2
        }

    query_tokens = tokens(query)
    intent_tokens = tokens(service_name)
    relevance_tokens = query_tokens | intent_tokens
    if not relevance_tokens:
        return []

    # Category narrows the candidate set, but can never make a record relevant
    # by itself. This prevents an unrelated record in the same sector from
    # bypassing remote discovery.
    candidates = (
        db.query(GovernmentService)
        .filter(
            GovernmentService.category.ilike(f"%{service_category}%")
            if service_category
            and service_category.lower() not in {"general", "civic_service"}
            else True
        )
        .limit(50)
        .all()
    )
    ranked = []
    for service in candidates:
        searchable_text = f"{service.name} {service.description}"
        matched_tokens = tokens(searchable_text) & relevance_tokens
        if not matched_tokens:
            continue
        score = len(matched_tokens)
        if tokens(service.name) & intent_tokens:
            score += len(tokens(service.name) & intent_tokens)
        ranked.append((score, service))

    ranked.sort(key=lambda result: (-result[0], result[1].service_code.lower()))
    return [service for _, service in ranked[:5]]


@router.post(
    "/assist",
    response_model=AutonomousAgentAssistanceResponse,
    status_code=status.HTTP_200_OK,
    summary="One-Shot Autonomous Pipeline: Intent + Discovery + Auto-Eligibility"
)
async def autonomous_assist(
    payload: AIUnderstandRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    total_start = time.perf_counter()
    clean_query = (payload.query or "").strip()
    logger.info(
        "===> [Pipeline Start] User ID: %s | Query: '%s'",
        current_user.id,
        _redact_query_for_log(clean_query),
    )

    # 1. Resolve Profile, State, District & Non-PII Demographic Context
    profile = db.query(CitizenProfile).filter(CitizenProfile.user_id == current_user.id).first()
    state = profile.state if (profile and profile.state) else ""
    district = profile.district if (profile and profile.district) else ""

    if not clean_query:
        intent = await gemini_service.understand_request(clean_query)
        return AutonomousAgentAssistanceResponse(
            query=payload.query,
            citizen_state=state,
            understood_intent=intent,
            matched_schemes=[]
        )

    demographic_context: Dict[str, Any] = {}
    if profile:
        actual_age = getattr(profile, "age", None)
        if actual_age is None and getattr(profile, "dob", None):
            try:
                birth_year = datetime.strptime(str(profile.dob).strip()[:10], "%Y-%m-%d").year
                actual_age = datetime.now().year - birth_year
            except Exception:
                actual_age = None

        demographic_context = {
            "age": actual_age,
            "gender": getattr(profile, "gender", None),
            "occupation_type": getattr(profile, "occupation_type", "student"),
            "education_level": getattr(profile, "education_level", None),
            "employment_type": getattr(profile, "employment_type", None),
            "nationality": getattr(profile, "nationality", "Indian")
        }

    # 2. Understand intent before deciding whether discovery is actionable
    t_llm = time.perf_counter()
    intent = await gemini_service.understand_request(clean_query)
    logger.info(f"===> [Intent Task Finished] in {time.perf_counter() - t_llm:.2f}s")

    intent_values = {
        str(getattr(intent, "service_category", "")).strip().lower(),
        str(getattr(intent, "service_name", "")).strip().lower(),
        str(getattr(intent, "required_action", "")).strip().lower()
    }
    is_non_actionable = (
        not intent_values
        or intent_values.issubset({
            "",
            "general",
            "general_inquiry",
            "conversational",
            "greeting",
            "none",
            "greet",
            "respond_to_greeting",
        })
    )
    if is_non_actionable:
        return AutonomousAgentAssistanceResponse(
            query=payload.query,
            citizen_state=state,
            understood_intent=intent,
            matched_schemes=[]
        )

    cached_records = _find_cached_services(
        db=db,
        query=clean_query,
        service_category=intent.service_category,
        service_name=intent.service_name,
    )
    if cached_records:
        logger.info(
            "[SchemeDiscovery] Local catalog hit (%d records); "
            "bypassing Gemini discovery.",
            len(cached_records),
        )
        discovery_targets = [(record, None) for record in cached_records]
    else:
        discovered = await discovery_service.discover_schemes(
            service_category=intent.service_category,
            service_name=intent.service_name,
            citizen_query=redact_sensitive_text(payload.query),
            user_state=state,
            user_district=district,
            demographics=demographic_context
        )
        unique_discovered = []
        seen_scheme_keys = set()
        for item in discovered:
            scheme_key = (
                item.service_code.strip().lower()
                or item.portal_url.strip().lower()
            )
            if scheme_key and scheme_key not in seen_scheme_keys:
                seen_scheme_keys.add(scheme_key)
                unique_discovered.append(item)
        discovered = unique_discovered

        service_codes = [item.service_code for item in discovered]
        existing_records = {
            service.service_code: service
            for service in db.query(GovernmentService).filter(
                GovernmentService.service_code.in_(service_codes)
            ).all()
        } if service_codes else {}
        discovery_targets = []
        for item in discovered:
            rules_dict = item.eligibility_criteria.model_dump()
            service_record = existing_records.get(item.service_code)
            if not service_record:
                service_record = GovernmentService(
                    service_code=item.service_code,
                    name=item.name,
                    category=item.category,
                    governance_tier=getattr(item, "governance_tier", "state"),
                    portal_url=item.portal_url,
                    description=item.description,
                    eligibility_rules=json.dumps(rules_dict),
                    required_documents=json.dumps(item.required_documents),
                    required_form_fields=json.dumps(
                        getattr(item, "required_form_fields", [])
                    ),
                )
                db.add(service_record)
                db.flush()
            else:
                updated = False
                if (
                    not getattr(service_record, "required_form_fields", None)
                    or service_record.required_form_fields == "[]"
                ):
                    service_record.required_form_fields = json.dumps(
                        getattr(item, "required_form_fields", [])
                    )
                    updated = True
                if not getattr(service_record, "governance_tier", None):
                    service_record.governance_tier = getattr(
                        item, "governance_tier", "state"
                    )
                    updated = True
                if (
                    service_record.category == "civic_service"
                    and item.category != "civic_service"
                ):
                    service_record.category = item.category
                    updated = True
                if updated:
                    db.flush()
            discovery_targets.append((service_record, item))

    # 3. Batch DB Persistence & Instant Auto-Eligibility Evaluation
    t_eval = time.perf_counter()
    evaluated_schemes: List[EvaluatedSchemeItem] = []

    if discovery_targets:
        for service_record, item in discovery_targets:
            existing_link = db.query(UserDiscoveredService).filter(
                UserDiscoveredService.user_id == current_user.id,
                UserDiscoveredService.service_id == service_record.id
            ).first()
            if not existing_link:
                db.add(UserDiscoveredService(
                    user_id=current_user.id,
                    service_id=service_record.id
                ))
            else:
                existing_link.discovered_at = func.now()

            # Deterministic Rule Evaluation against the citizen's profile
            eligibility_result = eligibility_service.evaluate(
                profile=profile,
                service=service_record
            )

            # Safely parse JSON strings for the response schema
            raw_docs = getattr(service_record, "required_documents", "[]")
            parsed_docs = _load_json_list(raw_docs)

            raw_fields = getattr(service_record, "required_form_fields", "[]")
            parsed_fields = _load_json_list(raw_fields)

            evaluated_schemes.append(
                EvaluatedSchemeItem(
                    id=service_record.id,
                    service_code=service_record.service_code,
                    name=service_record.name,
                    category=service_record.category,
                    governance_tier=getattr(service_record, "governance_tier", "state") or "state",
                    portal_url=service_record.portal_url,
                    description=service_record.description,
                    required_documents=parsed_docs,
                    required_form_fields=parsed_fields,
                    eligibility=eligibility_result,
                    capabilities=getattr(item, "capabilities", ["online_form"]),
                    status_tag=(
                        "CONFIRMED"
                        if eligibility_result.eligibility_status.value == "ELIGIBLE"
                        else "POTENTIALLY_RELEVANT"
                        if eligibility_result.eligibility_status.value == "POTENTIALLY_ELIGIBLE"
                        else "REQUIRES_FURTHER_INVESTIGATION"
                    ),
                    verified_benefits=getattr(item, "verified_benefits", []),
                    source_reference_links=getattr(item, "source_reference_links", []),
                    benefit_value=getattr(item, "benefit_value", 0) or 0,
                    deadline=getattr(item, "deadline", "") or ""
                )
            )

        db.commit()

        confidence_order = {
            "CONFIRMED": 0,
            "POTENTIALLY_RELEVANT": 1,
            "REQUIRES_FURTHER_INVESTIGATION": 2,
        }
        evaluated_schemes.sort(
            key=lambda scheme: (
                confidence_order.get(scheme.status_tag, 2),
                -(scheme.benefit_value or 0),
                scheme.deadline or "9999-12-31",
                calculate_capability_rank(scheme.capabilities),
                scheme.service_code.lower(),
            )
        )

    logger.info(f"===> [Persistence & Auto-Eligibility Completed] in {time.perf_counter() - t_eval:.3f}s")
    logger.info(f"===> [Total Pipeline Runtime] {time.perf_counter() - total_start:.2f}s")

    return AutonomousAgentAssistanceResponse(
        query=payload.query,
        citizen_state=state,
        understood_intent=intent,
        matched_schemes=evaluated_schemes
    )