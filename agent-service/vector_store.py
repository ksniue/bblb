"""Legacy Chroma indexing is disabled; use the RAG service document API."""

class VectorStoreService:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("Legacy Chroma indexing is disabled. Import documents through the authenticated RAG service; existing local databases were not modified.")

if __name__ == "__main__":
    VectorStoreService()
