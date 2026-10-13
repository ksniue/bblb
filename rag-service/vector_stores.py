"""Retrieval always requires a trusted principal and knowledge-base scope."""
class VectorStoreService:
    def __init__(self, store, principal, knowledge_base_id):
        principal.require("reader", knowledge_base_id)
        self.store, self.principal, self.knowledge_base_id = store, principal, knowledge_base_id

    def search(self, embedding):
        return self.store.search(self.principal, self.knowledge_base_id, embedding)
