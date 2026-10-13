from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    knowledge_base_id: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")

    @field_validator("question")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Question cannot be blank")
        return value.strip()


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str = Field(pattern=r"^(19|20)\d{2}-(0[1-9]|1[0-2])$")


class Consumable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    remaining_percent: float = Field(ge=0, le=100, allow_inf_nan=False)


class UsageRecord(BaseModel):
    """Contract implemented by the trusted enterprise usage API adapter."""
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    month: str = Field(pattern=r"^(19|20)\d{2}-(0[1-9]|1[0-2])$")
    cleaning_count: int = Field(ge=0, le=1000000, strict=True)
    cleaning_minutes: float = Field(ge=0, le=100000000, allow_inf_nan=False)
    area_sqm: float = Field(ge=0, le=1000000000, allow_inf_nan=False)
    consumables: list[Consumable] = Field(default_factory=list, max_length=30)
    source_version: str = Field(min_length=1, max_length=128)


class Recommendations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recommendations: list[str] = Field(min_length=1, max_length=5)

    @field_validator("recommendations")
    @classmethod
    def bounded(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 500 for value in values):
            raise ValueError("Invalid recommendation length")
        return values


class ReportResponse(BaseModel):
    answer: str
    facts: UsageRecord
    recommendations: list[str]
    request_id: str
    citations: list[dict[str, Any]] = Field(default_factory=list)


class Citation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    document_id: str = Field(max_length=128)
    version_id: str = Field(max_length=128)
    chunk_id: str = Field(max_length=128)
    filename: str = Field(max_length=512)
    chunk_index: int = Field(ge=0)


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    answer: str = Field(max_length=100000)
    citations: list[Citation] = Field(max_length=100)
    conversation_id: str = Field(max_length=128)
    request_id: str = Field(max_length=128)


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: str = Field(pattern=r"^(user|assistant)$")
    content: str = Field(max_length=100000)
    citations: list[Citation] = Field(default_factory=list, max_length=100)
    created_at: str = Field(max_length=64)


class ConversationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    conversation_id: str = Field(max_length=128)
    messages: list[ConversationMessage] = Field(max_length=200)
    limit: int = Field(ge=1, le=200)


class AssistantResponse(BaseModel):
    action: Literal["knowledge_qa", "generate_usage_report", "clarify"]
    answer: str
    request_id: str
    citations: list[Citation] = Field(default_factory=list)
    conversation_id: str | None = None
    report: ReportResponse | None = None
