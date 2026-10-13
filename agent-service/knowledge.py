"""Shared forwarding contract for direct chat and the controlled agent tool."""
import httpx
from pydantic import ValidationError

from service.auth import Principal
from service.config import Settings
from service.contracts import ChatRequest, ChatResponse
from service.upstream import ServiceError, request_json, require_success


def safe_rag_status(status: int) -> None:
    if status in {400, 401, 403, 404, 409, 422, 429}:
        raise ServiceError(status, "knowledge_request_rejected", "Knowledge request was rejected; check access, conversation, knowledge base, and request key")
    require_success(status)


async def forward_chat(settings: Settings, client: httpx.AsyncClient, principal: Principal,
                       token: str, body: ChatRequest, key: str, request_id: str) -> ChatResponse:
    if body.knowledge_base_id and body.knowledge_base_id not in principal.knowledge_base_ids:
        raise ServiceError(403, "knowledge_request_rejected", "Knowledge base is not authorized")
    status, result = await request_json(client, "POST", f"{settings.rag_base_url.rstrip('/')}/v1/chat",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key, "X-Request-ID": request_id},
        json=body.model_dump(exclude_none=True))
    safe_rag_status(status)
    try:
        response = ChatResponse.model_validate(result)
    except ValidationError:
        raise ServiceError(502, "invalid_knowledge_response", "Knowledge service returned an invalid answer") from None
    response.request_id = request_id
    return response
