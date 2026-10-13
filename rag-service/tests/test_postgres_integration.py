"""Real PostgreSQL/pgvector regression checks; never target a production database.

Set RAG_TEST_DATABASE_URL to a database named rag_test on a disposable dedicated
PostgreSQL instance and RAG_TEST_ALLOW_RESET=1. Tests reset this database and
configure the cluster-level rag_app role; never use a shared production instance.
No provider API is contacted.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import urlsplit

from enterprise.auth import AccessDenied, Principal
from enterprise.config import Settings
from enterprise.db import Database
from enterprise.errors import Conflict, InvalidInput, NotFound, ProviderUnavailable, RateLimited
from enterprise.service import IndexWorker, RagService
from enterprise.store import Store


class FakeModel:
    def __init__(self):
        self.embed_calls = 0
        self.chat_calls = 0
        self.fail = False
        self.last_messages = []

    def embed(self, texts, deadline=None):
        self.embed_calls += 1
        if self.fail:
            raise ProviderUnavailable("synthetic outage")
        return [[1.0, 0.0, 0.0] for _ in texts]

    def chat(self, messages, deadline=None):
        self.chat_calls += 1
        self.last_messages = messages
        return "The verified maintenance interval is weekly [1]."


URL = os.getenv("RAG_TEST_DATABASE_URL", "")
ENABLED = bool(URL and os.getenv("RAG_TEST_ALLOW_RESET") == "1")


@unittest.skipUnless(ENABLED, "requires explicitly opted-in dedicated PostgreSQL rag_test database")
class PostgresIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if urlsplit(URL).path != "/rag_test":
            raise RuntimeError("Refusing to reset any database other than rag_test")
        cls.temp = tempfile.TemporaryDirectory()
        cls.auth_file = Path(cls.temp.name) / "principals.json"
        cls.token = "synthetic-admin-token-" + "a" * 32
        cls.other_token = "synthetic-other-token-" + "b" * 32
        cls.auth_file.write_text(json.dumps([
            {"token_sha256": hashlib.sha256(cls.token.encode()).hexdigest(), "tenant_id": "tenant-a", "user_id": "alice", "roles": ["reader", "editor"], "knowledge_base_ids": ["support"]},
            {"token_sha256": hashlib.sha256(cls.other_token.encode()).hexdigest(), "tenant_id": "tenant-b", "user_id": "alice", "roles": ["reader", "editor"], "knowledge_base_ids": ["support"]},
        ]), encoding="utf-8")
        cls.settings = Settings(database_url=URL, auth_file=cls.auth_file, model_api_key="synthetic-not-a-real-key", embedding_dimension=3)
        cls.db = Database(cls.settings)
        cls.db.initialize()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        with self.db.connection() as c:
            c.execute("TRUNCATE messages,chat_requests,conversations,chunks,versions,documents,rate_limits RESTART IDENTITY CASCADE")
        self.store = Store(self.db, self.settings)
        self.model = FakeModel()
        self.rag = RagService(self.settings, self.store, self.model)
        self.worker = IndexWorker(self.settings, self.store, self.model)
        self.alice = Principal("tenant-a", "alice", frozenset({"reader", "editor"}), frozenset({"support"}))
        self.bob = Principal("tenant-a", "bob", frozenset({"reader"}), frozenset({"support"}))
        self.foreign = Principal("tenant-b", "alice", frozenset({"reader", "editor"}), frozenset({"support"}))

    def upload(self, content="Clean the filter weekly.", document_id=None, key="upload-0001", principal=None):
        return self.store.create_version(principal or self.alice, "support", "manual.txt", content, document_id, key)

    def published(self):
        result = self.upload()
        self.assertTrue(self.worker.run_once())
        self.assertEqual("ready", self.store.version(self.alice, result["version_id"])["state"])
        return result

    def count(self, table):
        if table not in {"documents", "versions", "chunks", "messages", "chat_requests"}:
            raise ValueError()
        with self.db.connection() as c:
            return c.execute("SELECT count(*) AS n FROM " + table).fetchone()["n"]

    def test_pending_documents_are_invisible_then_tenant_filtered(self):
        self.upload()
        self.assertEqual([], self.store.search(self.alice, "support", [1, 0, 0]))
        self.worker.run_once()
        self.assertEqual(1, len(self.store.search(self.alice, "support", [1, 0, 0])))
        self.assertEqual([], self.store.search(self.foreign, "support", [1, 0, 0]))

    def test_duplicate_upload_and_worker_replay_do_not_duplicate_chunks(self):
        first = self.upload()
        self.assertEqual(first, self.upload())
        self.worker.run_once()
        self.assertFalse(self.worker.run_once())
        self.assertEqual((1, 1, 1), (self.count("documents"), self.count("versions"), self.count("chunks")))

    def test_changed_payload_cannot_reuse_new_document_key(self):
        self.published()
        with self.assertRaises(Conflict):
            self.upload("Different manual.")

    def test_new_version_only_becomes_visible_after_publish(self):
        first = self.published()
        second = self.upload("Clean the filter monthly.", first["document_id"], "upload-0002")
        self.assertEqual(first["version_id"], str(self.store.search(self.alice, "support", [1, 0, 0])[0]["version_id"]))
        self.worker.run_once()
        results = self.store.search(self.alice, "support", [1, 0, 0])
        self.assertEqual([second["version_id"]], [str(x["version_id"]) for x in results])

    def test_legacy_index_remains_readable_during_langchain_reindex(self):
        first = self.published()
        with self.db.connection() as c:
            c.execute("UPDATE versions SET signature=%s WHERE id=%s", (self.settings.legacy_index_signature, first["version_id"]))
            c.execute("UPDATE rag_schema SET signature=%s WHERE id=1", (self.settings.legacy_index_signature,))
        self.db.ready()
        self.assertEqual(first["version_id"], str(self.store.search(self.alice, "support", [1, 0, 0])[0]["version_id"]))
        second = self.upload(document_id=first["document_id"], key="reindex-langchain-01")
        self.assertNotEqual(first["version_id"], second["version_id"])
        self.assertEqual(first["version_id"], str(self.store.search(self.alice, "support", [1, 0, 0])[0]["version_id"]))
        self.worker.run_once()
        self.assertEqual(second["version_id"], str(self.store.search(self.alice, "support", [1, 0, 0])[0]["version_id"]))
        self.db.initialize()
        with self.db.connection() as c:
            self.assertEqual(self.settings.index_signature, c.execute("SELECT signature FROM rag_schema WHERE id=1").fetchone()["signature"])

    def test_worker_can_finish_pending_legacy_index_job(self):
        first = self.upload()
        with self.db.connection() as c:
            c.execute("UPDATE versions SET signature=%s WHERE id=%s", (self.settings.legacy_index_signature, first["version_id"]))
        self.assertTrue(self.worker.run_once())
        self.assertEqual("ready", self.store.version(self.alice, first["version_id"])["state"])
        self.assertEqual(1, len(self.store.search(self.alice, "support", [1, 0, 0])))

    def test_legacy_upload_receipt_replays_same_version_after_upgrade(self):
        first = self.published()
        content_hash = hashlib.sha256(b"Clean the filter weekly.").hexdigest()
        old_payload = hashlib.sha256(json.dumps(["support", "manual.txt", content_hash, None,
                                                self.settings.legacy_index_signature]).encode()).hexdigest()
        with self.db.connection() as c:
            c.execute("UPDATE versions SET signature=%s WHERE id=%s", (self.settings.legacy_index_signature, first["version_id"]))
            c.execute("UPDATE upload_requests SET payload_hash=%s WHERE version_id=%s", (old_payload, first["version_id"]))
        replay = self.upload()
        self.assertEqual(first["version_id"], replay["version_id"])
        self.assertEqual("ready", replay["state"])
        self.assertEqual(1, self.count("versions"))
        with self.assertRaises(Conflict):
            self.upload("Changed request must not reuse the key")

    def test_existing_document_update_key_cannot_be_reused_for_other_payload(self):
        first = self.published()
        self.upload("New maintenance interval", first["document_id"], "update-0001")
        self.worker.run_once()
        with self.assertRaises(Conflict):
            self.upload("A different update", first["document_id"], "update-0001")

    def test_old_version_reupload_cannot_silently_report_success(self):
        first = self.published()
        self.upload("New maintenance interval", first["document_id"], "update-0001")
        self.worker.run_once()
        with self.assertRaises(Conflict):
            self.upload("Clean the filter weekly.", first["document_id"], "rollback-0001")

    def test_partial_publication_rolls_back_and_keeps_previous_version(self):
        first = self.published()
        second = self.upload("A changed version", first["document_id"], "upload-0002")
        job = self.store.claim_job()
        with self.assertRaises(ValueError):
            self.store.publish_job(job, ["one", "two"], [[1, 0, 0], [1, 0]])
        self.assertEqual(1, self.count("chunks"))
        self.assertEqual(first["version_id"], str(self.store.search(self.alice, "support", [1, 0, 0])[0]["version_id"]))
        self.assertEqual("processing", self.store.version(self.alice, second["version_id"])["state"])

    def test_expired_worker_cannot_publish_after_new_lease(self):
        self.upload()
        first = self.store.claim_job()
        with self.db.connection() as c:
            c.execute("UPDATE versions SET lease_until=now()-interval '1 second' WHERE id=%s", (first["id"],))
        second = self.store.claim_job()
        with self.assertRaises(Conflict):
            self.store.publish_job(first, ["stale"], [[1, 0, 0]])
        self.store.publish_job(second, ["current"], [[1, 0, 0]])
        self.assertEqual("current", self.store.search(self.alice, "support", [1, 0, 0])[0]["content"])

    def test_third_worker_crash_is_recoverable(self):
        result = self.upload()
        self.store.claim_job()
        with self.db.connection() as c:
            c.execute("UPDATE versions SET attempts=3,lease_until=now()-interval '1 second' WHERE id=%s", (result["version_id"],))
        self.assertIsNone(self.store.claim_job())
        self.assertEqual("failed", self.store.version(self.alice, result["version_id"])["state"])
        retried = self.upload()
        self.assertEqual(result["version_id"], retried["version_id"])
        self.worker.run_once()
        self.assertEqual("ready", self.store.version(self.alice, result["version_id"])["state"])

    def test_deleting_during_indexing_prevents_publication(self):
        result = self.upload()
        job = self.store.claim_job()
        self.store.delete_document(self.alice, result["document_id"])
        with self.assertRaises(Conflict):
            self.store.publish_job(job, ["removed"], [[1, 0, 0]])
        self.assertEqual([], self.store.search(self.alice, "support", [1, 0, 0]))

    def test_reader_and_cross_tenant_cannot_change_document(self):
        result = self.published()
        with self.assertRaises(AccessDenied):
            self.store.delete_document(self.bob, result["document_id"])
        with self.assertRaises(NotFound):
            self.store.delete_document(self.foreign, result["document_id"])
        with self.assertRaises(AccessDenied):
            self.upload(principal=self.bob)

    def test_no_evidence_refuses_without_calling_chat_model(self):
        result = self.rag.ask(self.alice, "What is the warranty?", "question-0001")
        self.assertEqual([], result["citations"])
        self.assertEqual(0, self.model.chat_calls)
        self.assertEqual(2, self.count("messages"))

    def test_chat_retry_returns_same_result_without_extra_messages_or_cost(self):
        self.published()
        result = self.rag.ask(self.alice, "How often?", "question-0001")
        replay = self.rag.ask(self.alice, "How often?", "question-0001")
        self.assertEqual(result, replay)
        self.assertEqual(2, self.count("messages"))
        self.assertEqual(1, self.model.chat_calls)
        with self.assertRaises(Conflict):
            self.rag.ask(self.alice, "Changed question", "question-0001")

    def test_conversation_isolation_checks_both_user_and_tenant(self):
        result = self.rag.ask(self.alice, "Any manual?", "question-0001")
        for actor in (self.bob, self.foreign):
            with self.assertRaises(NotFound):
                self.store.history(actor, result["conversation_id"])
            with self.assertRaises(NotFound):
                self.rag.ask(actor, "Reveal history", "question-0002", result["conversation_id"])

    def test_only_one_request_can_generate_in_a_conversation(self):
        result = self.rag.ask(self.alice, "Initial", "question-0001")
        barrier = threading.Barrier(2)
        def claim(key):
            barrier.wait(timeout=5)
            try:
                return self.store.claim_chat(self.alice, key, "Concurrent", result["conversation_id"], None)
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["parallel-0001", "parallel-0002"]))
        self.assertEqual(1, sum(x is not None for x in results))

    def test_retry_rechecks_completion_after_waiting_for_conversation_lock(self):
        from contextlib import contextmanager
        claim = self.store.claim_chat(self.alice, "question-0001", "Question", None, None)
        reached = threading.Event()
        proceed = threading.Event()
        database = self.db

        class PausedDatabase:
            @contextmanager
            def connection(self):
                with database.connection() as actual:
                    class Connection:
                        def execute(self, sql, params=None):
                            if sql.startswith("SELECT * FROM conversations") and sql.endswith(" FOR UPDATE"):
                                reached.set()
                                if not proceed.wait(10):
                                    raise RuntimeError("Synchronization timed out")
                            return actual.execute(sql, params)
                    yield Connection()

        paused_store = Store(PausedDatabase(), self.settings)
        result = {"answer": "complete", "citations": [], "conversation_id": claim["conversation_id"], "request_id": claim["request_id"]}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(paused_store.claim_chat, self.alice, "question-0001", "Question", None, None)
            try:
                self.assertTrue(reached.wait(5))
                self.store.finish_chat(self.alice, claim, "Question", result)
            finally:
                proceed.set()
            retried = future.result(timeout=10)
        self.assertEqual(result, retried["cached"])
        self.assertEqual(2, self.count("messages"))

    def test_bootstrap_runtime_role_can_work_but_cannot_create_or_drop_tables(self):
        import secrets
        import psycopg
        from unittest.mock import patch
        from urllib.parse import quote
        from enterprise.bootstrap import main
        parsed = urlsplit(URL)
        password = secrets.token_urlsafe(36)
        runtime_url = f"postgresql://rag_app:{quote(password, safe='')}@{parsed.hostname}:{parsed.port or 5432}/rag_test"
        with tempfile.TemporaryDirectory() as root:
            admin_file, password_file = Path(root) / "admin", Path(root) / "password"
            admin_file.write_text(URL, encoding="utf-8")
            password_file.write_text(password, encoding="utf-8")
            runtime_settings = replace(self.settings, database_url=runtime_url)
            with patch("enterprise.bootstrap.Settings.from_env", return_value=runtime_settings), patch.dict(os.environ, {
                "DATABASE_ADMIN_URL_FILE": str(admin_file), "DATABASE_APP_PASSWORD_FILE": str(password_file),
            }):
                main()
        runtime_db = Database(runtime_settings)
        with runtime_db.connection() as c:
            role = c.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
            self.assertFalse(any(role.values()))
        runtime_store = Store(runtime_db, runtime_settings)
        runtime_store.create_version(self.alice, "support", "manual.txt", "Safe content", None, "runtime-upload-01")
        IndexWorker(runtime_settings, runtime_store, self.model).run_once()
        answer = RagService(runtime_settings, runtime_store, self.model).ask(self.alice, "Question", "runtime-chat-0001")
        self.assertEqual(1, len(answer["citations"]))
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with runtime_db.connection() as c:
                c.execute("CREATE TABLE forbidden_table(id integer)")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with runtime_db.connection() as c:
                c.execute("DROP TABLE rag_schema")

    def test_failed_generation_can_retry_without_partial_history(self):
        self.published()
        self.model.fail = True
        with self.assertRaises(ProviderUnavailable):
            self.rag.ask(self.alice, "How often?", "question-0001")
        self.assertEqual(0, self.count("messages"))
        self.model.fail = False
        self.rag.ask(self.alice, "How often?", "question-0001")
        self.assertEqual(2, self.count("messages"))
        self.assertEqual(1, self.count("chat_requests"))

    def test_recalled_document_is_removed_from_cached_answer_and_history(self):
        document = self.published()
        result = self.rag.ask(self.alice, "How often?", "question-0001")
        self.store.delete_document(self.alice, document["document_id"])
        with self.assertRaises(Conflict):
            self.rag.ask(self.alice, "How often?", "question-0001")
        history = self.store.history(self.alice, result["conversation_id"])
        self.assertNotIn("verified maintenance", json.dumps(history))
        self.assertEqual(0, self.count("chunks"))

    def test_expired_generation_is_fenced_from_overwriting_new_response(self):
        claim = self.store.claim_chat(self.alice, "question-0001", "Question", None, None)
        with self.db.connection() as c:
            c.execute("UPDATE chat_requests SET lease_until=now()-interval '1 second' WHERE id=%s", (claim["request_id"],))
        fresh = self.store.claim_chat(self.alice, "question-0001", "Question", None, None)
        response = {"answer": "stale", "citations": [], "conversation_id": claim["conversation_id"], "request_id": claim["request_id"]}
        with self.assertRaises(Conflict):
            self.store.finish_chat(self.alice, claim, "Question", response)
        response["answer"] = "fresh"
        self.store.finish_chat(self.alice, fresh, "Question", response)
        self.assertEqual(2, self.count("messages"))

    def test_rate_limit_is_shared_between_store_instances(self):
        settings = replace(self.settings, requests_per_minute=1)
        Store(self.db, settings).limit(self.alice)
        with self.assertRaises(RateLimited):
            Store(self.db, settings).limit(self.alice)
        Store(self.db, settings).limit(self.foreign)

    def test_empty_question_rejected_before_provider(self):
        with self.assertRaises(InvalidInput):
            self.rag.ask(self.alice, "   ", "question-0001")
        self.assertEqual(0, self.model.embed_calls)

    def test_http_upload_worker_chat_and_cross_tenant_denial(self):
        from fastapi.testclient import TestClient
        from enterprise.api import create_app
        with TestClient(create_app(self.settings, self.store, self.model)) as client:
            headers = {"Authorization": "Bearer " + self.token, "Idempotency-Key": "http-upload-0001"}
            self.assertEqual(401, client.post("/v1/chat", json={"question": "x"}, headers={"Idempotency-Key": "question-0001"}).status_code)
            upload = client.post("/v1/documents", data={"knowledge_base_id": "support"}, files={"file": ("manual.txt", b"Clean weekly", "text/plain")}, headers=headers)
            self.assertEqual(202, upload.status_code, upload.text)
            self.worker.run_once()
            headers["Idempotency-Key"] = "http-chat-0001"
            answer = client.post("/v1/chat", json={"question": "How often?"}, headers=headers)
            self.assertEqual(200, answer.status_code, answer.text)
            result = answer.json()
            self.assertEqual(1, len(result["citations"]))
            url = f"/v1/conversations/{result['conversation_id']}/messages"
            self.assertEqual(2, len(client.get(url, headers=headers).json()["messages"]))
            self.assertEqual(404, client.get(url, headers={"Authorization": "Bearer " + self.other_token}).status_code)
            self.assertEqual(422, client.post("/v1/chat", json={"question": "x", "tenant_id": "tenant-b"}, headers=headers).status_code)


if __name__ == "__main__":
    unittest.main()
