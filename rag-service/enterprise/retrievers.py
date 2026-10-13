"""Request-scoped retrieval; authorization stays inside the SQL query."""
from typing import Any

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import PrivateAttr


class AuthorizedRetriever(BaseRetriever):
    _store: Any = PrivateAttr()
    _principal: Any = PrivateAttr()
    _kb_id: str = PrivateAttr()
    _embeddings: Any = PrivateAttr()

    def __init__(self, *, store, principal, kb_id, embeddings):
        super().__init__()
        self._store, self._principal = store, principal
        self._kb_id, self._embeddings = kb_id, embeddings

    def _get_relevant_documents(self, query, *, run_manager):
        self._principal.require("reader", self._kb_id)
        vector = self._embeddings.embed_query(query)
        rows = self._store.search(self._principal, self._kb_id, vector)
        return [Document(page_content=row["content"], metadata={
            "document_id": str(row["document_id"]), "version_id": str(row["version_id"]),
            "chunk_id": str(row["chunk_id"]), "filename": row["filename"],
            "chunk_index": row["ordinal"],
        }) for row in rows]
