"""Exercise actual LangChain execution with synthetic provider/database boundaries."""
import json
import time
import unittest

from enterprise.auth import AccessDenied, Principal
from enterprise.chains import build_rag_chain, invoke_rag_chain
from enterprise.ingestion import split_documents
from enterprise.llm_adapters import ProviderChatModel, ProviderEmbeddings
from enterprise.retrievers import AuthorizedRetriever
from enterprise.service import IndexWorker, RagService
from test_unit import FakeModel, FakeStore, PRINCIPAL, settings


def row(content="每周清理滤芯", **changes):
    return {"content": content, "document_id": "doc", "version_id": "v", "chunk_id": "c",
            "filename": "manual.txt", "ordinal": 0, **changes}


class RecordingModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.embedding_inputs, self.deadlines = [], []

    def embed(self, texts, deadline=None):
        self.embedding_inputs.append(texts)
        self.deadlines.append(deadline)
        return super().embed(texts, deadline)

    def chat(self, messages, deadline=None):
        self.deadlines.append(deadline)
        return super().chat(messages, deadline)


class LangChainTests(unittest.TestCase):
    def test_real_chain_retrieves_evidence_uses_history_and_preserves_deadline(self):
        cfg, store, model = settings(), FakeStore([row()]), RecordingModel()
        store.history = lambda *args: [{"role": "user", "content": "滤芯如何保养？"},
                                      {"role": "assistant", "content": "历史答复不是事实依据"}]
        result = RagService(cfg, store, model).ask(PRINCIPAL, "多久一次？", "request-001")
        self.assertEqual(model.embedding_inputs, [["滤芯如何保养？\n多久一次？"]])
        self.assertEqual(model.deadlines[0], model.deadlines[1])
        self.assertGreater(model.deadlines[0], time.monotonic())
        self.assertEqual(model.messages[1]["role"], "user")
        evidence = json.loads(model.messages[-1]["content"])
        self.assertEqual(evidence["retrieved_evidence"], [{"source": 1, "text": "每周清理滤芯"}])
        self.assertEqual(result["citations"][0]["version_id"], "v")

    def test_retriever_denies_scope_before_embedding_or_database_query(self):
        model, store = RecordingModel(), FakeStore([row()])
        retriever = AuthorizedRetriever(store=store, principal=PRINCIPAL, kb_id="unauthorized",
                                        embeddings=ProviderEmbeddings(model))
        with self.assertRaises(AccessDenied):
            retriever.invoke("reveal another knowledge base")
        self.assertEqual(model.embedding_inputs, [])

    def test_request_retrievers_keep_principals_separate(self):
        class ScopedStore(FakeStore):
            def search(self, principal, kb_id, vector):
                return [row(principal.tenant_id, document_id=principal.tenant_id)]
        other = Principal("other-tenant", "user", frozenset({"reader"}), frozenset({"kb"}))
        store, model = ScopedStore(), RecordingModel()
        first = build_rag_chain(settings(), store, model, PRINCIPAL, "kb", time.monotonic() + 90)
        second = build_rag_chain(settings(), store, model, other, "kb", time.monotonic() + 90)
        for chain, expected in [(first, "tenant"), (second, "other-tenant"), (first, "tenant")]:
            result = invoke_rag_chain(chain, {"query": "x", "question": "x", "history": []})
            self.assertEqual(result["documents"][0].page_content, expected)

    def test_context_budget_does_not_send_extra_evidence_or_metadata(self):
        model = RecordingModel()
        store = FakeStore([row("abcd"), row("efgh", chunk_id="second")])
        result = RagService(settings(context_chars=5), store, model).ask(PRINCIPAL, "{question}", "request-002")
        data = json.loads(model.messages[-1]["content"])
        self.assertEqual(data["question"], "{question}")
        self.assertEqual(data["retrieved_evidence"], [{"source": 1, "text": "abcd"}])
        self.assertEqual(len(result["citations"]), 1)
        self.assertNotIn("tenant_id", model.messages[-1]["content"])

    def test_recursive_splitter_keeps_metadata_and_honors_resource_limits(self):
        cfg = settings(chunk_chars=12, chunk_overlap=2)
        docs = split_documents("第一段说明滤芯。\n\n第二段说明保养。\n\n第三段说明维护。", cfg, metadata={"version_id": "v"})
        self.assertGreater(len(docs), 1)
        self.assertTrue(all(len(d.page_content) <= 12 for d in docs))
        self.assertTrue(all(d.metadata["version_id"] == "v" for d in docs))
        self.assertEqual([d.metadata["chunk_index"] for d in docs], list(range(len(docs))))
        from enterprise.errors import InvalidInput
        with self.assertRaises(InvalidInput):
            split_documents("x" * 40, settings(chunk_chars=4, chunk_overlap=1, max_chunks=2))

    def test_pending_legacy_job_uses_original_chunk_policy(self):
        cfg, store, model = settings(chunk_chars=4, chunk_overlap=1), FakeStore(), RecordingModel()
        store.claim_job = lambda: {"id": "v", "source_text": "abcdefghijk", "signature": cfg.legacy_index_signature}
        published = []
        store.publish_job = lambda job, chunks, vectors: published.append(chunks)
        self.assertTrue(IndexWorker(cfg, store, model).run_once())
        self.assertEqual(published, [["abcd", "defg", "ghij", "jk"]])

    def test_unknown_index_signature_fails_without_embedding_or_publication(self):
        store, model = FakeStore(), RecordingModel()
        store.claim_job = lambda: {"id": "v", "source_text": "data", "signature": "different-model:3"}
        IndexWorker(settings(), store, model).run_once()
        self.assertTrue(store.failed)
        self.assertFalse(store.published)
        self.assertEqual(model.embedding_inputs, [])

    def test_model_client_not_in_serialized_langchain_fields(self):
        model = RecordingModel()
        model.secret = "never-serialize-client-secret"
        wrapped = ProviderChatModel(model, 42)
        self.assertNotIn(model.secret, str(wrapped.model_dump()))
        self.assertNotIn(model.secret, repr(wrapped))


if __name__ == "__main__":
    unittest.main()
