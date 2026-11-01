from fastapi import APIRouter, Depends, status
from app.schemas.ai import AIUnderstandRequest, ServiceIntentResponse
from app.services.gemini_service import gemini_service
from app.auth_utils import get_current_user
from app.models.user import User

router = APIRouter(prefix="/api/ai", tags=["AI Request Understanding"])


@router.post(
    "/understand-request",
    response_model=ServiceIntentResponse,
    status_code=status.HTTP_200_OK,
    summary="Parse citizen natural language into structured service intent"
)
def understand_citizen_request(
    payload: AIUnderstandRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Parses unstructured citizen text using Gemini 2.5 Flash and returns
    structured public service classification without fabricating official rules.
    """
    return gemini_service.understand_request(payload.query)