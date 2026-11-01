import logging
import os
from datetime import datetime
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse
from app.schemas.agent_pipeline import (
    DiscoveredSchemeItem,
    DiscoveryResultContainer
)
from app.services.gemini_base import gemini_fast_client
from app.services.privacy import redact_sensitive_text
from app.config import settings

logger = logging.getLogger("agent.pipeline")

OFFICIAL_AUTHORITY_SUFFIXES = tuple(
    suffix.strip().lower().lstrip(".")
    for suffix in os.getenv("OFFICIAL_AUTHORITY_SUFFIXES", "gov.in,nic.in").split(",")
    if suffix.strip()
)

PRIMARY_DISCOVERY_PROMPT = """
You are an autonomous Government Scheme & Official Portal Discovery Engine for India.
Analyze the citizen's query, category, specific intent, and regional/demographic context.

CANONICAL CATEGORY RULES:
1. Set each scheme's category to exactly one of:
   ['education', 'transport', 'social_welfare', 'civic_service', 'agriculture', 'employment', 'healthcare'].
2. Classify by primary function. Scholarships, student fellowships, educational grants,
   and student financial portals are always 'education'.
3. Road, municipal, garbage, streetlight, and civic complaints are 'civic_service'.
4. Driving licences, LLR, vehicle RC, and transport services are 'transport'.
5. Pensions, disability aid, and ration cards are 'social_welfare'.
6. For every actionable request, search for the official transaction endpoint
   before returning an informational authority page.

CRITICAL PORTAL CAPABILITY RULES:
1. Identify direct interactive portals where citizens submit forms or complaints online.
2. Tag each portal with accurate capabilities selected from:
   ['online_form', 'document_upload', 'fee_payment', 'status_tracking', 'grievance_filing', 'information_only'].
3. If a portal is purely a static guide, circular, or departmental overview without online forms, tag it strictly with ['information_only'].
4. Prefer portals that support the citizen's requested transaction over informational directories.

JURISDICTION & GOVERNANCE TIER:
   - 'local': Municipal issues, road/drainage damage, streetlights, garbage, water supply. Anchor to citizen's district/city and state.
   - 'state': State-specific welfare, land records (Patta/Chitta), state electricity boards.
   - 'national': Central schemes and ministries.
   - 'hybrid': Issues with both central and state programs (e.g., scholarships). Return 1 Central portal (All India) AND 1 State-specific scheme.
DEMOGRAPHIC TARGETING:
   - Match eligibility using the provided citizen demographic context (age, gender, education, occupation, nationality).
   - Do NOT propose schemes whose age or gender criteria conflict with the provided demographic context.
Extract clean 'required_documents' and essential 'required_form_fields'.
Descriptions must be brief (under 25 words).
Represent scheme-specific eligibility as source-backed generic criteria with a
profile_field, rule_type, required_value or disallowed_values, label, and
source_url. Do not rely on the scheme name in the evaluation engine.
Use only criteria explicitly published on the official portal or its linked
official notification. Include the exact official source URL for every criterion.
If an eligibility fact cannot be verified from an official source, omit it or
mark it as requiring verification; never infer or invent it.
Every returned portal must include at least one official source reference link.

MULTI-SCHEME COVERAGE:
For broad farmer or agriculture requests, return distinct central and state
options covering the relevant sectors (income support, crop insurance, credit,
irrigation/mechanization, soil health, and organic farming) when authoritative
options exist. Do not invent benefits, deadlines, eligibility, or source links.
For every result, provide verified_benefits, source_reference_links, an
eligibility_confidence tag (CONFIRMED, POTENTIALLY_RELEVANT, or
REQUIRES_FURTHER_INVESTIGATION), and a machine-readable deadline only when
confirmed by an official source.
"""

AUTONOMOUS_RECOVERY_PROMPT = """
You are an expert Government Redressal & Public Administration Architect.
A previous automated lookup did not return a usable result.

Your task is to identify the current, canonical, most authoritative government
transaction portal or nodal authority that formally addresses this citizen issue.

CONSTRAINTS:
1. Select the most authoritative portal for the requested action.
2. Identify whether the portal supports online forms, document uploads, fee payment, status tracking, or grievance filing.
3. Use ['information_only'] only for a portal without an interactive submission workflow.
4. Return the direct transaction portal, not only a ministry landing page or directory.
5. Include the official portal URL and at least one official source reference URL.
6. Generate documents, form fields, benefits, deadlines, and eligibility rules
   only when explicitly supported by the official source. Never fill gaps from assumptions.
7. Use live web search grounding. Prefer the current canonical URL shown by the
   official authority over retired, redirected, or remembered legacy URLs.
"""

def calculate_capability_rank(capabilities: List[str]) -> int:
    caps = set(c.lower() for c in (capabilities or []))
    if "online_form" in caps and "document_upload" in caps:
        return 0
    if "online_form" in caps or "grievance_filing" in caps:
        return 1
    if "information_only" in caps:
        return 3
    return 2


def calculate_discovery_rank(scheme: DiscoveredSchemeItem) -> tuple:
    confidence_rank = {
        "CONFIRMED": 0,
        "POTENTIALLY_RELEVANT": 1,
        "REQUIRES_FURTHER_INVESTIGATION": 2,
    }.get((scheme.eligibility_confidence or "").upper(), 2)
    benefit_rank = -(scheme.benefit_value or 0)
    deadline_rank = 1
    if scheme.deadline:
        try:
            deadline_rank = 0 if datetime.fromisoformat(scheme.deadline).date() >= datetime.now().date() else 2
        except ValueError:
            deadline_rank = 1
    return (
        confidence_rank,
        benefit_rank,
        deadline_rank,
        calculate_capability_rank(scheme.capabilities),
        scheme.service_code.lower(),
    )


class SchemeDiscoveryService:
    @staticmethod
    def _is_official_url(url: str) -> bool:
        try:
            parsed = urlparse((url or "").strip())
            hostname = (parsed.hostname or "").lower().rstrip(".")
            if parsed.scheme != "https" or not hostname:
                return False
            return any(
                hostname == suffix or hostname.endswith(f".{suffix}")
                for suffix in OFFICIAL_AUTHORITY_SUFFIXES
            )
        except (AttributeError, ValueError):
            return False

    async def _autonomous_fallback_synthesis(
        self,
        service_category: str,
        service_name: str,
        citizen_query: str,
        user_state: str,
        user_district: str,
        demographics: Optional[Dict[str, Any]] = None
    ) -> List[DiscoveredSchemeItem]:
        """
        Dynamically derives the authoritative nodal department
        without hardcoded keyword rules.
        """
        logger.info("[SchemeDiscovery] Executing autonomous fallback synthesis...")

        demo_str = ""
        if demographics:
            demo_parts = [f"- {k.replace('_', ' ').title()}: {v}" for k, v in demographics.items() if v]
            if demo_parts:
                demo_str = "\nCitizen Demographic Context (Non-PII):\n" + "\n".join(demo_parts)

        safe_query = redact_sensitive_text(citizen_query)
        prompt = f"""
Identify the apex official Indian government authority/portal for this issue:
- Issue Category: {service_category}
- Specific Action/Service: {service_name}
- Citizen Query: {safe_query}
- State Jurisdiction: {user_state}
- District/City: {user_district}
{demo_str}

Return exactly 1 high-priority, strictly verified government service or portal.
"""

        result: DiscoveryResultContainer = await gemini_fast_client.execute_structured_query(
            task_name="AutonomousRecovery",
            prompt=prompt,
            system_instruction=AUTONOMOUS_RECOVERY_PROMPT,
            schema=DiscoveryResultContainer,
            use_search_grounding=settings.GEMINI_DISCOVERY_GROUNDING_ENABLED,
        )

        if not result or not result.schemes:
            logger.warning(
                "[SchemeDiscovery] Remote discovery returned no result; "
                "skipping autonomous recovery."
            )
            return []

        return [
            scheme for scheme in result.schemes
            if self._is_official_url(scheme.portal_url)
        ]

    async def discover_schemes(
        self,
        service_category: str,
        service_name: str,
        citizen_query: str,
        user_state: Optional[str] = None,
        user_district: Optional[str] = None,
        demographics: Optional[Dict[str, Any]] = None
    ) -> List[DiscoveredSchemeItem]:
        effective_state = user_state or ""
        effective_district = user_district or ""
        safe_query = redact_sensitive_text(citizen_query)

        demo_str = ""
        if demographics:
            demo_parts = [f"- {k.replace('_', ' ').title()}: {v}" for k, v in demographics.items() if v]
            if demo_parts:
                demo_str = "\nCitizen Demographic Context (Non-PII):\n" + "\n".join(demo_parts)

        prompt = f"""
Find authentic government portals, departments, or schemes for:
- Citizen Query: {safe_query}
- Service Category: {service_category}
- Specific Intent: {service_name}
- Citizen State Jurisdiction: {effective_state}
- Citizen District/City: {effective_district}
{demo_str}

INSTRUCTIONS:
1. Return portals that directly support the requested citizen action when available, and accurately populate each portal's capabilities.
2. If a portal is informational only, set capabilities to ['information_only'].
3. Use the supplied jurisdiction and demographics only when they are present.
4. Do not invent a department, portal, benefit, deadline, document, or form field.
5. Extract both 'required_documents' and 'required_form_fields' only when published by the official source.
6. Use live web search grounding to verify the current canonical transaction URL.
7. Do not return retired or remembered URLs when an official source shows a newer canonical portal.
"""

        result: DiscoveryResultContainer = await gemini_fast_client.execute_structured_query(
            task_name="SchemeDiscovery",
            prompt=prompt,
            system_instruction=PRIMARY_DISCOVERY_PROMPT,
            schema=DiscoveryResultContainer,
            use_search_grounding=settings.GEMINI_DISCOVERY_GROUNDING_ENABLED,
        )

        if result is None:
            logger.warning(
                "[SchemeDiscovery] Rate limit reached. "
                "Short-circuiting without calling autonomous recovery."
            )
            return []

        if not result.schemes:
            logger.warning(
                "[SchemeDiscovery] Remote discovery returned no schemes; "
                "skipping autonomous recovery."
            )
            return []

        valid_schemes: List[DiscoveredSchemeItem] = []

        if result and result.schemes:
            seen_codes = set()
            seen_urls = set()
            for scheme in result.schemes:
                service_code = scheme.service_code.strip().lower()
                portal_url = scheme.portal_url.strip().lower().rstrip("/")
                if service_code in seen_codes or portal_url in seen_urls:
                    continue
                if self._is_official_url(scheme.portal_url):
                    scheme.source_reference_links = [
                        link for link in scheme.source_reference_links
                        if self._is_official_url(link)
                    ]
                    if not scheme.source_reference_links:
                        scheme.eligibility_confidence = "REQUIRES_FURTHER_INVESTIGATION"
                    valid_schemes.append(scheme)
                    seen_codes.add(service_code)
                    seen_urls.add(portal_url)
                else:
                    logger.warning(
                        f"[SchemeDiscovery] Rejected non-official URL: {scheme.portal_url}"
                    )

        if valid_schemes:
            valid_schemes.sort(key=calculate_discovery_rank)
            return valid_schemes

        # Recovery is only valid when generation succeeded but every returned
        # portal failed official-source validation.
        fallback_schemes = await self._autonomous_fallback_synthesis(
            service_category=service_category,
            service_name=service_name,
            citizen_query=citizen_query,
            user_state=effective_state,
            user_district=effective_district,
            demographics=demographics
        )
        fallback_schemes.sort(key=lambda s: calculate_capability_rank(s.capabilities))
        return fallback_schemes


discovery_service = SchemeDiscoveryService()