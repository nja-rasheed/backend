from typing import List, Literal
from pydantic import BaseModel, ConfigDict, Field
from app.schemas.ai import ServiceIntentResponse
from app.schemas.eligibility import EligibilityCheckResponse


class ServiceIntentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_category: str = Field(default="general", description="e.g. grievance, education, civic_infrastructure, social_welfare")
    service_name: str = Field(default="general_inquiry", description="e.g. road_maintenance, higher_education_scholarship")
    user_type: str = Field(default="citizen", description="e.g. citizen, student, farmer")
    required_action: str = Field(default="none", description="e.g. file_complaint, check_eligibility, apply_for_subsidy")
    missing_information: List[str] = Field(default_factory=list)
    summary: str = Field(default="", description="One-line summary")


class SchemeCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_field: str = ""
    rule_type: str = "required"
    label: str = ""
    required_value: str = ""
    disallowed_values: List[str] = Field(default_factory=list)
    source_url: str = ""


class SchemeEligibilityRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_type: str = Field(default="citizen")
    nationality: str = Field(default="")
    state: str = Field(default="All India")
    target_state: str = Field(default="")
    target_role: str = Field(default="")
    min_age: int = Field(default=0)
    max_age: int = Field(default=0)
    gender: str = Field(default="")
    income_ceiling: float = Field(default=0)
    max_annual_family_income: float = Field(default=0)
    education_level: str = Field(default="")
    occupation: str = Field(default="")
    employment_type: str = Field(default="")
    min_marks_percentile: float = Field(default=0)
    source_url: str = ""
    criteria: List[SchemeCriterion] = Field(
        default_factory=list,
        description="Scheme-specific, source-backed rule definitions evaluated by the generic engine"
    )


class DiscoveredSchemeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_code: str
    name: str
    category: Literal[
        "education",
        "transport",
        "social_welfare",
        "civic_service",
        "agriculture",
        "employment",
        "healthcare"
    ]
    governance_tier: Literal["local", "state", "national", "hybrid"] = Field(
        default="state",
        description="'local' for municipal/roads, 'state' for state schemes, 'national' for pan-India, 'hybrid' for multi-tier schemes like scholarships"
    )
    portal_url: str
    description: str
    eligibility_criteria: SchemeEligibilityRules
    required_documents: List[str] = Field(default_factory=list)
    required_form_fields: List[str] = Field(
        default_factory=list,
        description="Key form fields required on the application/grievance form, e.g. ['full_name', 'landmark', 'rto_office']"
    )
    capabilities: List[str] = Field(
        default_factory=lambda: ["online_form"],
        description="Supported portal capabilities: 'online_form', 'document_upload', 'fee_payment', 'status_tracking', 'grievance_filing', or 'information_only'"
    )
    official_source: str = Field(default="Official Government Authority")
    source_reference_links: List[str] = Field(default_factory=list)
    verified_benefits: List[str] = Field(default_factory=list)
    eligibility_confidence: str = Field(default="REQUIRES_FURTHER_INVESTIGATION")
    benefit_value: float = 0
    deadline: str = ""


class DiscoveryResultContainer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemes: List[DiscoveredSchemeItem] = Field(default_factory=list)


# Unified scheme structure delivered to the frontend
class EvaluatedSchemeItem(BaseModel):
    id: int
    service_code: str
    name: str
    category: str
    governance_tier: str = "state"
    portal_url: str
    description: str
    required_documents: List[str] = Field(default_factory=list)
    required_form_fields: List[str] = Field(default_factory=list)
    eligibility: EligibilityCheckResponse
    capabilities: List[str] = Field(default_factory=list)
    status_tag: str = "REQUIRES_FURTHER_INVESTIGATION"
    verified_benefits: List[str] = Field(default_factory=list)
    source_reference_links: List[str] = Field(default_factory=list)
    benefit_value: float = 0
    deadline: str = ""


class AutonomousAgentAssistanceResponse(BaseModel):
    query: str
    citizen_state: str
    understood_intent: ServiceIntentResponse
    matched_schemes: List[EvaluatedSchemeItem]