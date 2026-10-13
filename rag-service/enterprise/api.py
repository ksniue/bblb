from contextlib import asynccontextmanager
import logging
import os
import time
import uuid

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .auth import AccessDenied, Authenticator
from .config import Settings
from .db import Database
from .errors import Conflict, InvalidInput, NotFound, ProviderUnavailable, RateLimited
from .model_client import ModelClient
from .service import RagService, decode_document, validate_key
from .store import Store


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None
    knowledge_base_id: str | None = Field(default=None, min_length=1, max_length=80)

    class Config:
        extra = "forbid"


def create_app(settings=None, store=None, model=None, auth=None):
    supplied = store is not None

    @asynccontextmanager
    async def lifespan(app):
        current = settings or Settings.from_env()
        database = Database(current)
        if not supplied:
            await run_in_threadpool(database.ready)
        app.state.settings = current
        app.state.auth = auth or Authenticator(current.auth_file)
        app.state.store = store or Store(database, current)
        app.state.model = model or ModelClient(current)
        app.state.rag = RagService(current, app.state.store, app.state.model)
        app.state.db = None if supplied else database
        yield
        if model is None:
            app.state.model.close()

    app = FastAPI(title="Authenticated enterprise RAG", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    origins = settings.cors_origins if settings else tuple(x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip())
    if "*" in origins:
        raise RuntimeError("CORS origins must be explicit")
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(origins), allow_credentials=False,
                           allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])

    @app.middleware("http")
    async def request_boundary(request, call_next):
        request_id, started = str(uuid.uuid4()), time.monotonic()
        request.state.request_id = request_id
        if request.method == "POST":
            limit = request.app.state.settings.max_upload_bytes + 65536 if request.url.path == "/v1/documents" else 32768
            length = request.headers.get("content-length")
            if length is None or not length.isdigit():
                return JSONResponse({"error": "Content-Length is required", "request_id": request_id}, status_code=411)
            if int(length) > limit:
                return JSONResponse({"error": "Request body exceeds limit", "request_id": request_id}, status_code=413)
            original_receive, received = request._receive, 0
            async def receive():
                nonlocal received
                message = await original_receive()
                received += len(message.get("body", b""))
                if received > limit:
                    raise InvalidInput("Request body exceeds limit")
                return message
            request._receive = receive
        try:
            response = await call_next(request)
        except Exception as exc:
            logging.error("request_failed request_id=%s type=%s", request_id, type(exc).__name__)
            response = JSONResponse({"error": "Internal service error", "request_id": request_id}, status_code=500)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        logging.info("request_complete request_id=%s status=%s elapsed_ms=%s", request_id, response.status_code, round((time.monotonic()-started)*1000))
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": "Invalid request fields", "request_id": request.state.request_id}, status_code=422)

    for error, status, label in ((AccessDenied, 403, "Access denied"), (NotFound, 404, "Resource not found"),
                                 (Conflict, 409, "Request conflicts with current state"), (InvalidInput, 422, "Invalid input"),
                                 (ProviderUnavailable, 503, "Model provider unavailable"), (RateLimited, 429, "Request limit exceeded")):
        async def handle(request, exc, code=status, message=label):
            return JSONResponse({"error": message, "request_id": request.state.request_id}, status_code=code)
        app.add_exception_handler(error, handle)

    def principal(request: Request, authorization: str | None = Header(default=None)):
        try:
            user = request.app.state.auth.authenticate(authorization)
        except AccessDenied:
            raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"}) from None
        request.app.state.store.limit(user)
        return user

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready(request: Request):
        try:
            if request.app.state.db:
                request.app.state.db.ready()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready"}

    @app.post("/v1/chat")
    def chat(body: ChatRequest, request: Request, user=Depends(principal), idempotency_key: str = Header(alias="Idempotency-Key")):
        return request.app.state.rag.ask(user, body.question, idempotency_key,
                                       str(body.conversation_id) if body.conversation_id else None, body.knowledge_base_id)

    @app.get("/v1/conversations/{conversation_id}/messages")
    def messages(conversation_id: uuid.UUID, request: Request, user=Depends(principal)):
        return {"conversation_id": str(conversation_id), "messages": request.app.state.store.history(user, str(conversation_id), bounded=False), "limit": 200}

    @app.post("/v1/documents", status_code=202)
    async def upload(request: Request, knowledge_base_id: str = Form(min_length=1, max_length=80), file: UploadFile = File(),
                     document_id: uuid.UUID | None = Form(default=None), user=Depends(principal),
                     idempotency_key: str = Header(alias="Idempotency-Key")):
        user.require("editor", knowledge_base_id)
        validate_key(idempotency_key)
        content = await file.read(request.app.state.settings.max_upload_bytes + 1)
        await file.close()
        filename, text = await run_in_threadpool(decode_document, file.filename or "", content, request.app.state.settings)
        return await run_in_threadpool(request.app.state.store.create_version, user, knowledge_base_id, filename, text,
                                       str(document_id) if document_id else None, idempotency_key)

    @app.get("/v1/knowledge-bases/{knowledge_base_id}/documents")
    def documents(knowledge_base_id: str, request: Request, user=Depends(principal)):
        return {"documents": request.app.state.store.documents(user, knowledge_base_id), "limit": 200}

    @app.get("/v1/versions/{version_id}")
    def version(version_id: uuid.UUID, request: Request, user=Depends(principal)):
        return request.app.state.store.version(user, str(version_id))

    @app.delete("/v1/documents/{document_id}", status_code=204)
    def delete(document_id: uuid.UUID, request: Request, user=Depends(principal)):
        request.app.state.store.delete_document(user, str(document_id))

    return app


app = create_app()
