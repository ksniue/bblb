from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from enterprise.auth import AccessDenied, Authenticator, Principal
from enterprise.config import Settings
from enterprise.errors import InvalidInput, ProviderUnavailable
from enterprise.model_client import ModelClient
from enterprise.service import RagService, IndexWorker, decode_document, split_document, validate_key
from enterprise.store import vector_literal


def settings(**changes):
    return replace(Settings(database_url="postgresql://unused", auth_file=Path("unused"), model_api_key="unused", embedding_dimension=3), **changes)


PRINCIPAL = Principal("tenant", "user", frozenset({"reader", "editor"}), frozenset({"kb"}))


class FakeModel:
    def __init__(self, answer="依据资料[1]", error=False):
        self.answer, self.error, self.chats = answer, error, 0

    def embed(self, texts, deadline=None):
        if self.error:
            raise ProviderUnavailable()
        return [[1., 0., 0.] for _ in texts]

    def chat(self, messages, deadline=None):
        self.chats += 1
        self.messages = messages
        return self.answer


class FakeStore:
    def __init__(self, docs=None):
        self.docs, self.finished, self.failed, self.published = docs or [], None, False, False

    def claim_chat(self, *args):
        return {"request_id": "request", "conversation_id": "conversation", "lease_token": "lease", "kb_id": "kb"}

    def history(self, *args):
        return []

    def search(self, *args):
        return self.docs

    def finish_chat(self, principal, claim, question, response):
        self.finished = response

    def fail_chat(self, claim):
        self.failed = True

    def claim_job(self):
        return {"id": "version", "source_text": "abc", "lease_token": "lease"}

    def renew_job(self, job):
        pass

    def publish_job(self, *args):
        self.published = True

    def fail_job(self, *args):
        self.failed = True


class UnitTests(unittest.TestCase):
    def test_authentication_map_and_scope(self):
        token = "random-test-value-that-is-over-thirty-two-characters"
        entry = {"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "tenant_id": "tenant", "user_id": "user",
                 "roles": ["reader"], "knowledge_base_ids": ["kb"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "principals.json"
            path.write_text(json.dumps([entry]))
            auth = Authenticator(path)
            self.assertEqual(auth.authenticate("bearer " + token).tenant_id, "tenant")
            for value in (None, "Bearer bad", "Bearer " + "z" * 40):
                with self.assertRaises(AccessDenied):
                    auth.authenticate(value)
            with self.assertRaises(AccessDenied):
                auth.authenticate("Bearer " + token).require("reader", "other")
            with self.assertRaises(AccessDenied):
                auth.authenticate("Bearer " + token).require("editor", "kb")

    def test_configuration_fails_without_secrets(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(RuntimeError):
            Settings.from_env()

    def test_document_encoding_type_limits_and_chunks(self):
        cfg = settings(chunk_chars=4, chunk_overlap=1, max_document_chars=20)
        self.assertEqual(decode_document("../x.txt", b"abcdef", cfg), ("x.txt", "abcdef"))
        self.assertEqual(split_document("abcdef", cfg), ["abcd", "def"])
        for filename, content in (("x.txt", b"\xff"), ("x.pdf", b"%PDF-malformed"), ("x.txt", b""), ("x.txt", b"x" * 21)):
            with self.assertRaises(InvalidInput):
                decode_document(filename, content, cfg)

    def test_invalid_keys_and_embeddings_rejected(self):
        for key in ("", "short", "../../unsafe", "hello world"):
            with self.assertRaises(InvalidInput):
                validate_key(key)
        for vector in ([], [float("nan")], [float("inf")], [True]):
            with self.assertRaises(ValueError):
                vector_literal(vector)

    def test_no_evidence_does_not_call_chat(self):
        store, model = FakeStore(), FakeModel()
        result = RagService(settings(), store, model).ask(PRINCIPAL, "question", "request-1")
        self.assertEqual(result["citations"], [])
        self.assertEqual(model.chats, 0)
        self.assertIsNotNone(store.finished)

    def test_failure_does_not_commit_history(self):
        store = FakeStore()
        with self.assertRaises(ProviderUnavailable):
            RagService(settings(), store, FakeModel(error=True)).ask(PRINCIPAL, "question", "request-1")
        self.assertTrue(store.failed)
        self.assertIsNone(store.finished)

    def test_citations_checked_and_only_used_sources_returned(self):
        docs = [{"document_id": f"doc{i}", "version_id": "v", "chunk_id": f"c{i}", "filename": "x.txt", "ordinal": i, "content": "资料"} for i in range(3)]
        for answer in ("unsupported answer", "bad citation[9]"):
            result = RagService(settings(), FakeStore(docs), FakeModel(answer)).ask(PRINCIPAL, "question", "request-1")
            self.assertEqual(result["citations"], [])
            self.assertNotEqual(result["answer"], answer)
        result = RagService(settings(), FakeStore(docs), FakeModel("answer[3] and[1]")).ask(PRINCIPAL, "question", "request-1")
        self.assertEqual(result["answer"], "answer[1] and[2]")
        self.assertEqual([c["chunk_id"] for c in result["citations"]], ["c2", "c0"])

    def test_worker_does_not_publish_failed_embedding(self):
        store = FakeStore()
        IndexWorker(settings(), store, FakeModel(error=True)).run_once()
        self.assertTrue(store.failed)
        self.assertFalse(store.published)

    def test_provider_response_validation(self):
        model = ModelClient(settings())
        for reason in ("length", "tool_calls", "content_filter", None):
            with patch.object(model, "_post", return_value={"choices": [{"finish_reason": reason, "message": {"content": "answer"}}]}):
                with self.assertRaises(ProviderUnavailable):
                    model.chat([])
        with patch.object(model, "_post", return_value={"data": [{"index": 0, "embedding": [1, 2]}]}):
            with self.assertRaises(ProviderUnavailable):
                model.embed(["text"])


if __name__ == "__main__":
    unittest.main()
