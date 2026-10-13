import asyncio
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Coroutine, TypeVar, Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from service.auth import IdentityStore, Principal, current_principal
from service.assistant import AssistantService
from service.config import Settings
from service.contracts import AssistantResponse, ChatRequest, ChatResponse, ConversationResponse, ReportRequest, ReportResponse
from service.knowledge import forward_chat, safe_rag_status
from service.reports import ReportService
from service.rate_limit import WindowLimiter
from service.upstream import ServiceError, request_json, require_success

logger = logging.getLogger("agent_service")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
T = TypeVar("T")


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configured = (settings or Settings.from_env()).validate()
        app.state.settings = configured
        app.state.identities = IdentityStore(configured.identity_file)
        app.state.capacity = asyncio.Semaphore(configured.max_concurrent_requests)
        app.state.rate_limit = WindowLimiter(configured.requests_per_minute)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(configured.upstream_timeout_seconds, connect=min(5.0, configured.upstream_timeout_seconds)),
            limits=httpx.Limits(max_connections=configured.max_concurrent_requests,
                               max_keepalive_connections=configured.max_concurrent_requests),
            follow_redirects=False, trust_env=False, transport=transport,
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(title="Enterprise Robot Assistant", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def metadata_and_body_limit(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        started = time.monotonic()
        try:
            if request.method in {"POST", "PUT", "PATCH"}:
                content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise ServiceError(415, "unsupported_media_type", "JSON body required")
                data = bytearray()
                async def read_body():
                    async for chunk in request.stream():
                        data.extend(chunk)
                        if len(data) > 32768:
                            raise ServiceError(413, "request_too_large", "Request body exceeded 32 KiB")
                await asyncio.wait_for(read_body(), timeout=5.0)
                request._body = bytes(data)
            response = await call_next(request)
        except ServiceError as exc:
            response = JSONResponse({"error": {"code": exc.code, "message": exc.message}, "request_id": request.state.request_id}, status_code=exc.status)
        except asyncio.TimeoutError:
            response = JSONResponse({"error": {"code": "request_timeout", "message": "Request exceeded its deadline"}, "request_id": request.state.request_id}, status_code=504)
        except Exception:
            logger.error("request_failed request_id=%s", request.state.request_id)
            response = JSONResponse({"error": {"code": "internal_error", "message": "The request could not be completed"}, "request_id": request.state.request_id}, status_code=500)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        logger.info("request request_id=%s status=%s duration_ms=%s", request.state.request_id,
                    response.status_code, round((time.monotonic() - started) * 1000))
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}, "request_id": request.state.request_id}, status_code=exc.status)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": {"code": "request_rejected", "message": exc.detail}, "request_id": request.state.request_id}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"error": {"code": "invalid_request", "message": "Request fields are invalid",
                             "fields": [{"location": list(error["loc"]), "type": error["type"]} for error in exc.errors()]},
                             "request_id": request.state.request_id}, status_code=422)

    async def bounded(request: Request, operation: Coroutine[Any, Any, T]) -> T:
        if request.url.path.startswith("/v1/"):
            principal = request.state.principal
            if not request.app.state.rate_limit.take(principal.tenant_id, principal.user_id):
                operation.close()
                raise ServiceError(429, "rate_limit_exceeded", "Per-user request budget exceeded; retry after one minute")
        acquired = False
        try:
            try:
                await asyncio.wait_for(request.app.state.capacity.acquire(), timeout=0.05)
                acquired = True
            except asyncio.TimeoutError:
                operation.close()
                raise ServiceError(429, "service_busy", "The service is at capacity; retry later") from None
            try:
                return await asyncio.wait_for(operation, timeout=request.app.state.settings.request_timeout_seconds)
            except asyncio.TimeoutError:
                raise ServiceError(504, "request_timeout", "Request exceeded its total execution budget") from None
        finally:
            if acquired:
                request.app.state.capacity.release()
            else:
                operation.close()

    def forward_headers(request: Request) -> dict[str, str]:
        return {"Authorization": "Bearer " + request.headers["Authorization"].partition(" ")[2],
                "X-Request-ID": request.state.request_id}

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request, principal: Principal = Depends(current_principal)):
        async def check():
            status, _ = await request_json(request.app.state.client, "GET", f"{request.app.state.settings.rag_base_url.rstrip('/')}/health/ready")
            require_success(status)
            return {"status": "ready", "reports_enabled": request.app.state.settings.reports_enabled,
                    "assistant_enabled": request.app.state.settings.assistant_enabled,
                    "usage_dependency": "validated_configuration_only"}
        return await bounded(request, check())

    @app.post("/v1/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request, principal: Principal = Depends(current_principal)):
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
            raise HTTPException(422, "Idempotency-Key must contain 8 to 128 letters, digits, periods, underscores, colons, or hyphens")
        if body.knowledge_base_id and body.knowledge_base_id not in principal.knowledge_base_ids:
            raise HTTPException(403, "Knowledge base is not authorized")
        return await bounded(request, forward_chat(request.app.state.settings, request.app.state.client,
            principal, request.headers["Authorization"].partition(" ")[2], body, key, request.state.request_id))

    @app.post("/v1/assistant", response_model=AssistantResponse)
    async def assistant(body: ChatRequest, request: Request, principal: Principal = Depends(current_principal)):
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
            raise HTTPException(422, "Idempotency-Key must contain 8 to 128 letters, digits, periods, underscores, colons, or hyphens")
        service = AssistantService(request.app.state.settings, request.app.state.client)
        return await bounded(request, service.answer(principal,
            request.headers["Authorization"].partition(" ")[2], body, key, request.state.request_id))

    @app.get("/v1/conversations/{conversation_id}/messages", response_model=ConversationResponse)
    async def messages(conversation_id: str, request: Request, principal: Principal = Depends(current_principal)):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", conversation_id):
            raise HTTPException(422, "Invalid conversation ID")
        async def forward():
            status, result = await request_json(request.app.state.client, "GET",
                f"{request.app.state.settings.rag_base_url.rstrip('/')}/v1/conversations/{conversation_id}/messages",
                headers=forward_headers(request))
            safe_rag_status(status)
            try:
                return ConversationResponse.model_validate(result)
            except ValidationError:
                raise ServiceError(502, "invalid_knowledge_response", "Knowledge service returned invalid conversation history") from None
        return await bounded(request, forward())

    @app.post("/v1/reports", response_model=ReportResponse)
    async def report(body: ReportRequest, request: Request, principal: Principal = Depends(current_principal)):
        service = ReportService(request.app.state.settings, request.app.state.client)
        return await bounded(request, service.generate(principal, body.month, request.state.request_id))

    return app


app = create_app()
