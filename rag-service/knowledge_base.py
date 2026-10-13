"""Knowledge writes require a principal and versioned PostgreSQL store."""
from enterprise.service import decode_document, validate_key

class KnowledgeBaseService:
    def __init__(self, settings, store, principal):
        self.settings, self.store, self.principal = settings, store, principal

    def upload(self, data, filename, knowledge_base_id, idempotency_key, document_id=None):
        self.principal.require("editor", knowledge_base_id)
        validate_key(idempotency_key)
        name, text = decode_document(filename, data, self.settings)
        return self.store.create_version(self.principal, knowledge_base_id, name, text, document_id, idempotency_key)

if __name__ == "__main__":
    raise SystemExit("Use authenticated POST /v1/documents; anonymous ingestion is disabled.")
