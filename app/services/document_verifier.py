import json
import logging
from typing import Dict, List, Set
from app.models.document import CitizenDocument
from app.schemas.document import SemanticReadinessResult
from app.services.gemini_base import gemini_fast_client

logger = logging.getLogger("agent.documents")

DOCUMENT_MATCHER_PROMPT = """
You are an expert Indian Government Bureaucracy & Document Verification Specialist.
Your task is to semantically match a list of official required documents against a citizen's uploaded document vault.

RULES:
1. Match semantically, taking into account Indian administrative conventions:
   - "Aadhaar / Aadhar / UIDAI / UID" refer to the same identity document.
   - "Smart Ration Card / Family Card / TNEPDS" are equivalent.
   - "FIR / LDR / Police CSR" satisfy police reporting requirements.
   - "DL / Driving License" match driving credential proofs.
   - "Community Certificate / Caste Certificate" are equivalent.
2. Do NOT falsely match unrelated documents (e.g., an "Income Certificate" cannot satisfy an "Aadhaar Card" requirement).
3. If no uploaded document satisfies the requirement, set `matched_uploaded_id` to 0
   and `matched_filename` to an empty string.
4. Never assign the same uploaded document ID to more than one required document.
"""


class DocumentSemanticVerifier:
    @staticmethod
    def _normalise(value: object) -> str:
        return "".join(
            character.lower()
            for character in str(value or "")
            if character.isalnum()
        )

    @classmethod
    def _validate_matches(
        cls,
        required_docs: List[str],
        uploaded_docs: List[CitizenDocument],
        result: SemanticReadinessResult
    ) -> SemanticReadinessResult:
        documents_by_id: Dict[int, CitizenDocument] = {
            document.id: document for document in uploaded_docs
        }
        documents_by_text = {
            cls._normalise(document.document_type): document
            for document in uploaded_docs
        }
        used_ids: Set[int] = set()
        result_by_requirement = {
            item.required_document: item for item in result.matches
        }
        validated = []

        for requirement in required_docs:
            item = result_by_requirement.get(requirement)
            document = None
            if item and item.matched_uploaded_id in documents_by_id:
                document = documents_by_id[item.matched_uploaded_id]

            if document and document.id in used_ids:
                document = None

            if document:
                used_ids.add(document.id)
                validated.append(item.model_copy(update={
                    "matched_uploaded_id": document.id,
                    "matched_filename": document.original_filename,
                }))
                continue

            requirement_key = cls._normalise(requirement)
            exact_document = documents_by_text.get(requirement_key)
            if exact_document and exact_document.id not in used_ids:
                used_ids.add(exact_document.id)
                validated.append(item.model_copy(update={
                    "matched_uploaded_id": exact_document.id,
                    "matched_filename": exact_document.original_filename,
                }) if item else {
                    "required_document": requirement,
                    "matched_uploaded_id": exact_document.id,
                    "matched_filename": exact_document.original_filename,
                    "confidence_reasoning": "Exact document type match."
                })
            else:
                validated.append({
                    "required_document": requirement,
                    "matched_uploaded_id": 0,
                    "matched_filename": "",
                    "confidence_reasoning": "No unique uploaded document matched this requirement."
                })

        return SemanticReadinessResult(matches=validated)

    async def match_readiness(
        self,
        required_docs: List[str],
        uploaded_docs: List[CitizenDocument]
    ) -> SemanticReadinessResult:
        if not required_docs:
            return SemanticReadinessResult(matches=[])

        if not uploaded_docs:
            # If vault is empty, all required documents are marked missing without an LLM call
            return SemanticReadinessResult(
                matches=[
                    {
                        "required_document": req,
                        "matched_uploaded_id": 0,
                        "matched_filename": "",
                        "confidence_reasoning": "No documents uploaded in vault."
                    }
                    for req in required_docs
                ]
            )

        vault_payload = [
            {
                "id": doc.id,
                "document_type": doc.document_type or "",
                "original_filename": doc.original_filename or ""
            }
            for doc in uploaded_docs
            if doc.id is not None
        ]

        prompt = f"""
Official Scheme Required Documents:
{json.dumps(required_docs, indent=2)}

Citizen Uploaded Vault:
{json.dumps(vault_payload, indent=2)}

Evaluate each required document and identify if any uploaded document satisfies it.
"""

        result = await gemini_fast_client.execute_structured_query(
            task_name="SemanticDocMatch",
            prompt=prompt,
            system_instruction=DOCUMENT_MATCHER_PROMPT,
            schema=SemanticReadinessResult
        )

        if not result:
            result = SemanticReadinessResult(matches=[])

        return self._validate_matches(required_docs, uploaded_docs, result)


semantic_verifier = DocumentSemanticVerifier()