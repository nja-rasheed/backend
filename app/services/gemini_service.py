import logging
from app.schemas.ai import ServiceIntentResponse
from app.schemas.agent_pipeline import ServiceIntentResult
from app.services.gemini_base import gemini_fast_client
from app.services.privacy import redact_sensitive_text

logger = logging.getLogger("agent.pipeline")

INTENT_SYSTEM_PROMPT = """
You are an expert Government Bureaucracy and Public Service AI Intent Classifier.
Analyze the citizen's query and extract their true public service intent.

RULES:
1. Accurately assign service_category, service_name, user_type, and required_action from the query.
2. Keep the summary concise and useful to the citizen.
3. Never return meta-commentary such as "automated intent extraction", "classification",
   "the citizen asked", or a third-person analysis.
4. For actionable requests, summarize the requested public service directly.

CONVERSATIONAL & GREETING HANDLING:
- If the citizen's query is a greeting, casual check-in, polite pleasantry, or meta-question
  or other non-actionable conversation:
  - Do not invent a service or start scheme discovery.
  - Phrase summary as a direct, first-person, friendly reply that acknowledges the
    citizen and invites them to describe a public service, scheme, application, or grievance.
"""


class GeminiService:
    async def understand_request(self, query: str) -> ServiceIntentResponse:
        clean_query = redact_sensitive_text((query or "").strip())
        if not clean_query:
            return ServiceIntentResponse(
                service_category="general",
                service_name="general_inquiry",
                user_type="citizen",
                required_action="respond_to_greeting",
                missing_information=[],
                summary="Please describe the government service, scheme, application, or grievance you need help with."
            )

        result: ServiceIntentResult = await gemini_fast_client.execute_structured_query(
            task_name="IntentExtraction",
            prompt=clean_query,
            system_instruction=INTENT_SYSTEM_PROMPT,
            schema=ServiceIntentResult
        )

        if result:
            return ServiceIntentResponse(
                service_category=result.service_category,
                service_name=result.service_name,
                user_type=result.user_type,
                required_action=result.required_action,
                missing_information=result.missing_information,
                summary=result.summary
            )

        # Keep failed extraction non-actionable so discovery never runs on an
        # unclassified request.
        return ServiceIntentResponse(
            service_category="general",
            service_name="general_inquiry",
            user_type="citizen",
            required_action="respond_to_greeting",
            missing_information=[],
            summary="I’m ready to help you find a government service, scheme, application, or grievance process."
        )


gemini_service = GeminiService()