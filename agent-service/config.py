import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _url(name: str, value: str, *, internal: bool = False) -> str:
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not contain credentials, query, or fragment")
    if parsed.scheme != "https" and not internal:
        raise ValueError(f"{name} requires HTTPS; allow HTTP only on a trusted internal network")
    return value


@dataclass(frozen=True)
class Settings:
    identity_file: Path
    rag_base_url: str
    reports_enabled: bool = False
    usage_api_base_url: str = ""
    usage_api_token: str = ""
    model_base_url: str = ""
    model_api_key: str = ""
    model_name: str = ""
    request_timeout_seconds: float = 120.0
    upstream_timeout_seconds: float = 110.0
    max_concurrent_requests: int = 16
    model_max_tokens: int = 800
    allow_internal_http: bool = False
    allow_model_http: bool = False
    requests_per_minute: int = 30
    assistant_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        def boolean(name: str, default: str) -> bool:
            raw = os.getenv(name, default).strip().lower()
            if raw not in {"true", "false"}:
                raise ValueError(f"{name} must be true or false")
            return raw == "true"

        def secret(name: str) -> str:
            filename = os.getenv(name + "_FILE")
            if filename and os.getenv(name):
                raise ValueError(f"Set only one of {name} and {name}_FILE")
            return Path(filename).read_text(encoding="utf-8").strip() if filename else os.getenv(name, "").strip()

        identity = os.getenv("AUTH_PRINCIPALS_FILE", "").strip()
        if not identity:
            raise ValueError("AUTH_PRINCIPALS_FILE is required; no anonymous or demo fallback is available")
        reports_enabled = boolean("REPORTS_ENABLED", "false")
        assistant_enabled = boolean("ASSISTANT_ENABLED", "false")
        return cls(
            identity_file=Path(identity),
            rag_base_url=os.getenv("RAG_BASE_URL", ""),
            reports_enabled=reports_enabled,
            assistant_enabled=assistant_enabled,
            usage_api_base_url=os.getenv("USAGE_API_BASE_URL", ""),
            usage_api_token=secret("USAGE_API_TOKEN") if reports_enabled else "",
            model_base_url=os.getenv("MODEL_BASE_URL", ""),
            model_api_key=secret("MODEL_API_KEY") if reports_enabled or assistant_enabled else "",
            model_name=os.getenv("MODEL_NAME", ""),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "120")),
            upstream_timeout_seconds=float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "110")),
            max_concurrent_requests=int(os.getenv("MAX_CONCURRENT_REQUESTS", "16")),
            model_max_tokens=int(os.getenv("MODEL_MAX_TOKENS", "800")),
            allow_internal_http=boolean("ALLOW_INTERNAL_HTTP", "false"),
            allow_model_http=boolean("ALLOW_MODEL_HTTP", "false"),
            requests_per_minute=int(os.getenv("REQUESTS_PER_MINUTE", "30")),
        ).validate()

    def validate(self) -> "Settings":
        _url("RAG_BASE_URL", self.rag_base_url, internal=self.allow_internal_http)
        if not self.identity_file.is_file():
            raise ValueError("AUTH_PRINCIPALS_FILE must reference a readable identity mapping")
        if not 1 <= self.max_concurrent_requests <= 1024:
            raise ValueError("MAX_CONCURRENT_REQUESTS must be between 1 and 1024")
        if not 1 <= self.upstream_timeout_seconds <= self.request_timeout_seconds <= 180:
            raise ValueError("Timeouts must satisfy 1 <= upstream <= request <= 180")
        if not 64 <= self.model_max_tokens <= 4096:
            raise ValueError("MODEL_MAX_TOKENS must be between 64 and 4096")
        if not 1 <= self.requests_per_minute <= 10000:
            raise ValueError("REQUESTS_PER_MINUTE must be between 1 and 10000")
        if self.reports_enabled:
            _url("USAGE_API_BASE_URL", self.usage_api_base_url, internal=self.allow_internal_http)
            if not self.usage_api_token:
                raise ValueError("Reports require USAGE_API_TOKEN")
        if self.reports_enabled or self.assistant_enabled:
            _url("MODEL_BASE_URL", self.model_base_url, internal=self.allow_model_http)
            if not self.model_api_key or not self.model_name:
                raise ValueError("Reports and assistant require MODEL_API_KEY and MODEL_NAME")
        return self
