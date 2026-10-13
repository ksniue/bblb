"""Knowledge retrieval is owned by the authenticated RAG service."""

class RagSummarizeService:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("Local unauthenticated RAG is disabled. Configure RAG_BASE_URL and use /v1/chat.")
