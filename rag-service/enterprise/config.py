from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    database_url: str
    auth_file: Path
    model_api_key: str
    model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    chat_model: str = "qwen-plus"
    embedding_model: str = "text-embedding-v4"
    embedding_dimension: int = 1024
    max_upload_bytes: int = 5 * 1024 * 1024
    max_document_chars: int = 300_000
    chunk_chars: int = 1000
    chunk_overlap: int = 100
    max_chunks: int = 400
    max_question_chars: int = 4000
    context_chars: int = 14000
    history_chars: int = 12000
    output_tokens: int = 1000
    model_timeout: float = 45
    lease_seconds: int = 300
    requests_per_minute: int = 30
    top_k: int = 6
    max_cosine_distance: float = .45
    cors_origins: tuple = ()

    @property
    def index_signature(self):
        return f"{self.embedding_model}:{self.embedding_dimension}:recursive-{self.chunk_chars}-{self.chunk_overlap}:v2"

    @property
    def legacy_index_signature(self):
        return f"{self.embedding_model}:{self.embedding_dimension}:chars-{self.chunk_chars}-{self.chunk_overlap}:v1"

    @property
    def readable_index_signatures(self):
        # Only the splitter changed; these two signatures use identical embeddings.
        return (self.index_signature, self.legacy_index_signature)

    @classmethod
    def from_env(cls):
        def secret(name):
            direct, filename = os.getenv(name, "").strip(), os.getenv(name + "_FILE", "").strip()
            if direct and filename:
                raise RuntimeError(name + " and its _FILE alternative cannot both be set")
            return Path(filename).read_text(encoding="utf-8").strip() if filename else direct
        database_url, model_key = secret("DATABASE_URL"), secret("MODEL_API_KEY")
        if not database_url or not model_key or not os.environ.get("AUTH_PRINCIPALS_FILE", "").strip():
            raise RuntimeError("Missing production configuration: DATABASE_URL, AUTH_PRINCIPALS_FILE and MODEL_API_KEY are required")
        value = cls(
            database_url=database_url,
            auth_file=Path(os.environ["AUTH_PRINCIPALS_FILE"]).resolve(),
            model_api_key=model_key,
            model_base_url=os.getenv("MODEL_BASE_URL", cls.model_base_url).rstrip("/"),
            chat_model=os.getenv("CHAT_MODEL", cls.chat_model),
            embedding_model=os.getenv("EMBEDDING_MODEL", cls.embedding_model),
            embedding_dimension=int(os.getenv("EMBEDDING_DIMENSION", "1024")),
            max_cosine_distance=float(os.getenv("MAX_COSINE_DISTANCE", ".45")),
            cors_origins=tuple(x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()),
        )
        if not value.database_url.startswith(("postgresql://", "postgres://")):
            raise RuntimeError("DATABASE_URL must point to PostgreSQL")
        if urlparse(value.model_base_url).scheme != "https":
            raise RuntimeError("MODEL_BASE_URL must use HTTPS")
        if not 1 <= value.embedding_dimension <= 2000 or not 0 <= value.max_cosine_distance <= 2:
            raise RuntimeError("Invalid embedding dimension or retrieval threshold")
        if "*" in value.cors_origins:
            raise RuntimeError("CORS origins must be explicit")
        if not value.auth_file.is_file():
            raise RuntimeError("AUTH_FILE must be an existing mounted secret file")
        return value
