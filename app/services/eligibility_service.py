
import json
import logging
import re
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict
from app.models.profile import CitizenProfile
from app.models.service import GovernmentService
from app.schemas.eligibility import (
    CriterionEvaluation,
    CriterionStatus,
    EligibilityCheckResponse,
    EligibilityStatus,
)
from app.services.gemini_base import gemini_fast_client

logger = logging.getLogger("agent.eligibility")


def normalize_text(val: Optional[str]) -> str:
    if not val:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(val).lower().strip())


def _parse_numeric_value(val: object) -> Optional[float]:
    numeric_match = re.search(
        r"-?\d+(?:\.\d+)?",
        str(val).replace(",", "")
    )
    return float(numeric_match.group()) if numeric_match else None


def _rule_values(value: object) -> List[object]:
    if isinstance(value, list):
        return value
    return [value]


def _matches_allowed_value(actual: object, required: object) -> bool:
    actual_text = normalize_text(str(actual))
    return any(
        actual_text == normalize_text(str(candidate))
        or normalize_text(str(candidate)) in actual_text
        or actual_text in normalize_text(str(candidate))
        for candidate in _rule_values(required)
        if candidate not in (None, "")
    )


def _matches_user_type(profile: CitizenProfile, required: object) -> bool:
    required_values = _rule_values(required)
    occupation = getattr(profile, "occupation_type", None)
    if occupation and _matches_allowed_value(occupation, required_values):
        return True

    general_profile_roles = {"citizen", "resident", "applicant", "individual"}
    return any(
        normalize_text(str(value)) in general_profile_roles
        for value in required_values
        if value not in (None, "")
    )


def _canonical_profile_field(value: object) -> str:
    field = normalize_text(str(value or ""))
    aliases = {
        "educationalqualification": "education_level",
        "qualification": "education_level",
        "education": "education_level",
        "educationlevel": "education_level",
        "targetstate": "state",
        "targetrole": "occupation_type",
    }
    return aliases.get(field, field)


def _deduplicate_breakdown(
    breakdown: List[CriterionEvaluation],
) -> List[CriterionEvaluation]:
    merged: dict[str, CriterionEvaluation] = {}
    for criterion in breakdown:
        key = _canonical_profile_field(criterion.criterion)
        existing = merged.get(key)
        if existing is None:
            merged[key] = criterion
            continue

        required_values = [existing.required, criterion.required]
        required = existing.required
        if existing.required != criterion.required:
            required = required_values

        preferred = criterion
        if existing.passed and not criterion.passed:
            preferred = criterion
        elif not existing.passed and criterion.passed:
            preferred = existing

        remarks = existing.remarks
        if criterion.remarks not in remarks:
            remarks = f"{remarks} {criterion.remarks}".strip()

        merged[key] = preferred.model_copy(update={
            "criterion": existing.criterion,
            "required": required,
            "passed": existing.passed and criterion.passed,
            "remarks": remarks,
            "source_url": existing.source_url or criterion.source_url,
        })

    return list(merged.values())


class SemanticEquivalenceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_equivalent_or_satisfies: bool
    reason: str


async def check_semantic_education_match(required: str, actual: str) -> bool:
    if not required or not actual:
        return False

    prompt = f"""
Compare these two educational qualifications in the context of government schemes:
- Requirement: "{required}"
- Applicant Qualification: "{actual}"

Does the applicant's qualification satisfy or match the requirement?
Account for abbreviations, equivalent degrees, qualification tiers, regional terminology,
and standard educational ladders. Return true only when the applicant meets or matches
the requirement, otherwise return false.
"""
    result: Optional[SemanticEquivalenceSchema] = (
        await gemini_fast_client.execute_structured_query(
            task_name="EducationSemanticEquivalence",
            prompt=prompt,
            system_instruction=(
                "You are an educational qualification equivalency engine for public "
                "services. Evaluate qualification equivalence accurately."
            ),
            schema=SemanticEquivalenceSchema
        )
    )
    return bool(result and result.is_equivalent_or_satisfies)


@lru_cache(maxsize=256)
def _cached_semantic_education_match(required: str, actual: str) -> bool:
    coroutine = check_semantic_education_match(required, actual)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


class EligibilityService:

    @staticmethod
    def _calculate_age(profile: CitizenProfile) -> Optional[int]:
        """
        Calculate age using DOB.
        Falls back to the stored age if DOB is unavailable or invalid.
        """

        if profile.dob:
            try:
                dob = date.fromisoformat(str(profile.dob).strip())
                today = date.today()

                age = today.year - dob.year

                if (today.month, today.day) < (dob.month, dob.day):
                    age -= 1

                return age

            except (ValueError, TypeError):
                logger.warning("Invalid DOB format in citizen profile.")

        return profile.age

    @staticmethod
    def evaluate(
        profile: Optional[CitizenProfile],
        service: GovernmentService
    ) -> EligibilityCheckResponse:

        # Load rules & documents from stored JSON
        rules = (
            json.loads(service.eligibility_rules)
            if isinstance(service.eligibility_rules, str)
            else (service.eligibility_rules or {})
        )

        docs = (
            json.loads(service.required_documents)
            if isinstance(service.required_documents, str)
            else (service.required_documents or [])
        )

        breakdown: List[CriterionEvaluation] = []
        missing_data: List[str] = []
        overall_eligible = True

        # If user has no profile filled out yet
        if not profile:
            return EligibilityCheckResponse(
                service_id=service.id,
                service_name=service.name,
                service_code=service.service_code,
                is_eligible=False,
                eligibility_status=EligibilityStatus.POTENTIALLY_ELIGIBLE,
                evaluation_breakdown=[],
                required_documents_to_prepare=docs,
                missing_profile_data=["citizen_profile_not_found"],
                next_action_instructions="Complete your citizen profile to verify eligibility."
            )

        # 1. State / Residency Verification
        req_state = rules.get("state") or rules.get("target_state")

        if req_state and normalize_text(str(req_state)) not in {"allindia", "national", "central"}:
            actual_state = profile.state or "Unknown"

            passed = _matches_allowed_value(actual_state, req_state)

            if not passed:
                overall_eligible = False

            breakdown.append(CriterionEvaluation(
                criterion="State / Jurisdiction Residency",
                required=req_state,
                actual=actual_state,
                passed=passed,
                remarks="Resident jurisdiction matches." if passed else f"Only valid for residents of {req_state}."
            ))

        # 2. User type and target beneficiary verification
        required_user_type = rules.get("user_type")
        if required_user_type:
            passed = _matches_user_type(profile, required_user_type)
            actual_user_type = profile.occupation_type or "citizen"
            if not passed:
                overall_eligible = False

            breakdown.append(CriterionEvaluation(
                criterion="Applicant Type",
                required=required_user_type,
                actual=actual_user_type,
                passed=passed,
                remarks=(
                    "Applicant type matches."
                    if passed
                    else "Applicant type does not match the published requirement."
                )
            ))

        target_role = rules.get("target_role") or rules.get("occupation")

        if target_role:
            actual_role = profile.occupation_type or ""

            passed = _matches_allowed_value(actual_role, target_role)

            if not passed:
                overall_eligible = False

            breakdown.append(CriterionEvaluation(
                criterion="Target Beneficiary Role",
                required=str(target_role).title(),
                actual=actual_role.title(),
                passed=passed,
                remarks=f"Beneficiary role matches ({target_role})." if passed else f"This service is strictly for {target_role}s."
            ))

        # 3. Income Ceiling Verification
        max_income = next(
            (
                value for value in (
                    rules.get("max_annual_family_income"),
                    rules.get("income_ceiling"),
                    rules.get("annual_family_income"),
                    rules.get("max_income"),
                )
                if value not in (None, "", 0)
            ),
            None
        )

        if max_income is not None:
            actual_income = profile.annual_family_income

            if actual_income is None:
                missing_data.append("annual_family_income")
                overall_eligible = False

                breakdown.append(CriterionEvaluation(
                    criterion="Annual Family Income Ceiling",
                    required=f"<= Rs. {max_income}",
                    actual="Not provided in profile",
                    passed=False,
                    remarks="Income detail missing from profile."
                ))

            else:
                required_income = _parse_numeric_value(max_income)
                profile_income = _parse_numeric_value(actual_income)

                passed = (
                    required_income is not None
                    and profile_income is not None
                    and profile_income <= required_income
                )

                if not passed:
                    overall_eligible = False

                breakdown.append(CriterionEvaluation(
                    criterion="Annual Family Income Ceiling",
                    required=f"<= Rs. {max_income}",
                    actual=f"Rs. {actual_income}",
                    passed=passed,
                    remarks="Income is within the permissible limit." if passed else "Annual income exceeds the permissible limit."
                ))

        # 4. Educational Qualification Check
        req_edu = (
            rules.get("education_level")
            or rules.get("educational_qualification")
            or rules.get("qualification")
        )

        if req_edu:
            is_student = (profile.occupation_type or "").lower() == "student"
            actual_edu = profile.education_level or ""

            if is_student and not actual_edu:
                missing_data.append("education_level")
                overall_eligible = False

                breakdown.append(CriterionEvaluation(
                    criterion="Educational Qualification",
                    required=req_edu,
                    actual="Missing in profile",
                    passed=False,
                    remarks="Student profile must include education level."
                ))

            elif actual_edu:
                passed = _matches_allowed_value(actual_edu, req_edu)
                if not passed:
                    passed = _cached_semantic_education_match(
                        req_edu,
                        actual_edu
                    )

                if not passed:
                    overall_eligible = False

                breakdown.append(CriterionEvaluation(
                    criterion="Educational Qualification",
                    required=req_edu,
                    actual=actual_edu,
                    passed=passed,
                    remarks="Educational criteria satisfied." if passed else f"Requires {req_edu} qualification."
                ))

            else:
                breakdown.append(CriterionEvaluation(
                    criterion="Educational Qualification",
                    required=req_edu,
                    actual="Non-student profile (N/A)",
                    passed=True,
                    remarks="Educational qualification waived for non-student occupation."
                ))

        # 5. Worker Employment Verification
        req_emp_type = rules.get("employment_type")

        if req_emp_type:
            actual_emp_type = profile.employment_type or ""

            if not actual_emp_type:
                missing_data.append("employment_type")
                overall_eligible = False

                breakdown.append(CriterionEvaluation(
                    criterion="Employment Classification",
                    required=req_emp_type,
                    actual="Missing in profile",
                    passed=False,
                    remarks="Worker profile must include employment classification."
                ))

            else:
                passed = _matches_allowed_value(actual_emp_type, req_emp_type)

                if not passed:
                    overall_eligible = False

                breakdown.append(CriterionEvaluation(
                    criterion="Employment Classification",
                    required=req_emp_type,
                    actual=actual_emp_type,
                    passed=passed,
                    remarks="Employment type matches." if passed else f"Requires {req_emp_type} status."
                ))

        # 6. Minimum and Maximum Age Verification
        min_age = rules.get("min_age") or None
        max_age = rules.get("max_age") or None

        if min_age is not None or max_age is not None:

            actual_age = EligibilityService._calculate_age(profile)

            required_age = (
                f"{min_age if min_age is not None else 0}"
                f" - "
                f"{max_age if max_age is not None else 'No upper limit'} years"
            )

            if actual_age is None:
                missing_data.append("age")
                overall_eligible = False

                breakdown.append(CriterionEvaluation(
                    criterion="Age Eligibility",
                    required=required_age,
                    actual="Age not provided",
                    passed=False,
                    remarks="Date of birth or age is required to verify eligibility."
                ))

            else:
                passed = True

                if min_age is not None and actual_age < int(min_age):
                    passed = False

                if max_age is not None and actual_age > int(max_age):
                    passed = False

                if not passed:
                    overall_eligible = False

                breakdown.append(CriterionEvaluation(
                    criterion="Age Eligibility",
                    required=required_age,
                    actual=f"{actual_age} years",
                    passed=passed,
                    remarks=(
                        "Age requirement satisfied."
                        if passed
                        else f"Applicant must satisfy the age requirement of {required_age}."
                    )
                ))

        # 7. Gender Eligibility Verification
        req_gender = rules.get("gender")

        if req_gender and normalize_text(str(req_gender)) not in {"all", "any"}:
            actual_gender = (profile.gender or "").strip().lower()

            if not actual_gender:
                missing_data.append("gender")
                overall_eligible = False

                breakdown.append(CriterionEvaluation(
                    criterion="Gender Eligibility",
                    required=str(req_gender).title(),
                    actual="Not provided",
                    passed=False,
                    remarks="Gender information is required to verify eligibility."
                ))

            else:
                passed = _matches_allowed_value(actual_gender, req_gender)

                if not passed:
                    overall_eligible = False

                breakdown.append(CriterionEvaluation(
                    criterion="Gender Eligibility",
                    required=req_gender.title(),
                    actual=actual_gender.title(),
                    passed=passed,
                    remarks=(
                        "Gender requirement satisfied."
                        if passed
                        else f"This scheme is applicable to {req_gender} applicants."
                    )
                ))

        source_url = (
            rules.get("source_url")
            or rules.get("official_source")
            or service.portal_url
        )

        handled_rule_keys = {
            "state",
            "target_state",
            "user_type",
            "target_role",
            "occupation",
            "max_annual_family_income",
            "income_ceiling",
            "annual_family_income",
            "max_income",
            "education_level",
            "educational_qualification",
            "qualification",
            "employment_type",
            "min_age",
            "max_age",
            "gender",
            "source_url",
            "official_source",
            "criteria",
        }
        profile_field_aliases = {
            "target_state": "state",
            "target_role": "occupation_type",
        }

        for rule_name, required_value in rules.items():
            if rule_name in handled_rule_keys or required_value in (None, "", [], {}):
                continue

            profile_field = profile_field_aliases.get(rule_name, rule_name)
            actual_value = getattr(profile, profile_field, None)
            if profile_field == "nationality" and not actual_value:
                actual_value = "Indian"
            label = str(rule_name).replace("_", " ").title()
            has_value = actual_value is not None and str(actual_value).strip() != ""
            passed = False
            requires_verification = not has_value

            actual_number = _parse_numeric_value(actual_value) if has_value else None
            required_numbers = [
                _parse_numeric_value(value)
                for value in _rule_values(required_value)
            ]
            if has_value and actual_number is not None and any(
                number is not None for number in required_numbers
            ):
                required_number = next(
                    number for number in required_numbers if number is not None
                )
                if rule_name.startswith("min_"):
                    passed = actual_number >= required_number
                elif rule_name.startswith("max_"):
                    passed = actual_number <= required_number
                else:
                    passed = actual_number == required_number
                requires_verification = False
            elif has_value:
                passed = _matches_allowed_value(actual_value, required_value)
                requires_verification = False

            if not passed and not requires_verification:
                overall_eligible = False
            if requires_verification:
                missing_data.append(profile_field)

            breakdown.append(CriterionEvaluation(
                criterion=label,
                required=required_value,
                actual=actual_value if has_value else "Not provided",
                passed=passed,
                remarks=(
                    "Criterion satisfied."
                    if passed
                    else "Additional profile information is required to verify this criterion."
                    if requires_verification
                    else "Criterion is not satisfied."
                ),
                source_url=source_url
            ))

        # 8. Source-backed, scheme-specific rules expressed as data.
        for rule in rules.get("criteria", []) or []:
            if not isinstance(rule, dict):
                continue

            profile_field = str(rule.get("profile_field") or "").strip()
            label = str(rule.get("label") or profile_field or "Additional scheme criterion")
            rule_type = str(rule.get("rule_type") or "required").strip().lower()
            criterion_source_url = rule.get("source_url") or source_url
            actual_value = getattr(profile, profile_field, None) if profile_field else None
            if profile_field == "nationality" and not actual_value:
                actual_value = "Indian"
            has_value = actual_value is not None and str(actual_value).strip() != ""
            passed = False
            requires_verification = False

            if rule_type in {"required", "required_profile_field"}:
                expected = rule.get("required_value")
                passed = has_value and (
                    not expected or _matches_allowed_value(actual_value, expected)
                )
                requires_verification = not has_value
            elif rule_type in {"exclusion", "excluded_value"}:
                disallowed = rule.get("disallowed_values", [])
                if not isinstance(disallowed, list):
                    disallowed = [disallowed]
                passed = has_value and not any(
                    normalize_text(str(actual_value)) == normalize_text(str(value))
                    for value in disallowed
                )
                requires_verification = not has_value
            elif rule_type in {"minimum", "maximum"}:
                actual_number = _parse_numeric_value(actual_value) if has_value else None
                required_number = _parse_numeric_value(
                    rule.get("required_value")
                )
                if actual_number is None or required_number is None:
                    requires_verification = True
                else:
                    passed = (
                        actual_number >= required_number
                        if rule_type == "minimum"
                        else actual_number <= required_number
                    )
            else:
                expected = rule.get("required_value")
                passed = has_value and (
                    normalize_text(str(actual_value))
                    == normalize_text(str(expected))
                )
                requires_verification = not has_value

            if not passed and not requires_verification:
                overall_eligible = False
            if requires_verification:
                missing_data.append(profile_field or label)

            breakdown.append(CriterionEvaluation(
                criterion=label,
                required=rule.get("required_value")
                or rule.get("disallowed_values")
                or "Verified source-backed criterion",
                actual=actual_value if has_value else "Not provided",
                passed=passed,
                remarks=(
                    "Criterion satisfied."
                    if passed
                    else "Additional information or verification is required."
                    if requires_verification
                    else "Criterion is not satisfied."
                ),
                source_url=criterion_source_url
            ))

        # 9. Universal Access Fallback
        if not breakdown:
            breakdown.append(CriterionEvaluation(
                criterion="Public Service Access",
                required="Open to all citizens",
                actual="Citizen",
                passed=True,
                remarks="Universal access service. No demographic or occupational restrictions."
            ))

        normalized_breakdown = []
        for criterion in _deduplicate_breakdown(breakdown):
            actual_text = str(criterion.actual or "").strip().lower()
            if criterion.passed:
                criterion_status = CriterionStatus.PASSED
            elif (
                not actual_text
                or "missing" in actual_text
                or "not provided" in actual_text
                or "not available" in actual_text
                or "required to verify" in criterion.remarks.lower()
            ):
                criterion_status = CriterionStatus.REQUIRES_VERIFICATION
            else:
                criterion_status = CriterionStatus.FAILED

            normalized_breakdown.append(
                criterion.model_copy(update={
                    "status": criterion_status,
                    "source_url": criterion.source_url or source_url,
                })
            )

        has_failed_criterion = any(
            item.status == CriterionStatus.FAILED
            for item in normalized_breakdown
        )
        has_unverified_criterion = any(
            item.status == CriterionStatus.REQUIRES_VERIFICATION
            for item in normalized_breakdown
        )
        if has_failed_criterion:
            eligibility_status = EligibilityStatus.INELIGIBLE
        elif has_unverified_criterion or missing_data:
            eligibility_status = EligibilityStatus.POTENTIALLY_ELIGIBLE
        else:
            eligibility_status = EligibilityStatus.ELIGIBLE

        # Action instructions
        if eligibility_status == EligibilityStatus.ELIGIBLE:
            instructions = f"You are eligible. Prepare the required documents and proceed to {service.portal_url} to apply."
        elif eligibility_status == EligibilityStatus.POTENTIALLY_ELIGIBLE:
            instructions = "Baseline criteria match, but additional profile information or verification is required before applying."
        else:
            instructions = "You do not satisfy one or more criteria for this scheme. Review the breakdown."

        return EligibilityCheckResponse(
            service_id=service.id,
            service_name=service.name,
            service_code=service.service_code,
            is_eligible=eligibility_status == EligibilityStatus.ELIGIBLE,
            eligibility_status=eligibility_status,
            evaluation_breakdown=normalized_breakdown,
            required_documents_to_prepare=docs,
            missing_profile_data=missing_data,
            next_action_instructions=instructions
        )


eligibility_service = EligibilityService()
