"""Agent -> real RAG API -> real PostgreSQL, with synthetic external providers.

Use the same dedicated rag_test opt-in as the RAG integration suite. Do not run
this suite in parallel with other tests that reset that database.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient

from service.config import Settings as AgentSettings
from service.main import create_app as agent_app


URL = os.getenv("RAG_TEST_DATABASE_URL", "")
ENABLED = bool(URL and os.getenv("RAG_TEST_ALLOW_RESET") == "1")


class FakeModel:
    def __init__(self):
        self.chat_calls = 0

    def embed(self, texts, deadline=None):
        return [[1.0, 0.0, 0.0] for _ in texts]

    def chat(self, messages, deadline=None):
        self.chat_calls += 1
        return "Clean the filter weekly [1]."


@unittest.skipUnless(ENABLED, "requires dedicated rag_test database and explicit reset opt-in")
class CrossServiceIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if urlsplit(URL).path != "/rag_test":
            raise RuntimeError("Refusing non-test database")
        rag_root = Path(os.environ.get("RAG_PROJECT_PATH", str(Path(__file__).resolve().parents[2] / "rag-service"))).resolve()
        if not (rag_root / "enterprise" / "api.py").is_file():
            raise RuntimeError("Set RAG_PROJECT_PATH to the upgraded RAG project")
        sys.path.insert(0, str(rag_root))
        from enterprise.api import create_app
        from enterprise.auth import Principal
        from enterprise.config import Settings
        from enterprise.db import Database
        from enterprise.service import IndexWorker
        from enterprise.store import Store
        cls.create_rag = staticmethod(create_app)
        cls.Principal, cls.IndexWorker, cls.Store = Principal, IndexWorker, Store
        cls.temp = tempfile.TemporaryDirectory()
        cls.auth_file = Path(cls.temp.name) / "principals.json"
        cls.token = "cross-service-synthetic-token-" + "a" * 32
        cls.foreign_token = "cross-service-foreign-token-" + "b" * 32
        entries = []
        for token, tenant in [(cls.token, "tenant-a"), (cls.foreign_token, "tenant-b")]:
            entries.append({"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "tenant_id": tenant, "user_id": "alice", "roles": ["reader", "editor"], "knowledge_base_ids": ["support"]})
        cls.auth_file.write_text(json.dumps(entries), encoding="utf-8")
        cls.rag_settings = Settings(database_url=URL, auth_file=cls.auth_file, model_api_key="synthetic", embedding_dimension=3)
        cls.db = Database(cls.rag_settings)
        cls.db.initialize()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        with self.db.connection() as c:
            c.execute("TRUNCATE messages,chat_requests,conversations,chunks,versions,documents,rate_limits RESTART IDENTITY CASCADE")
        self.store = self.Store(self.db, self.rag_settings)
        self.model = FakeModel()
        self.principal = self.Principal("tenant-a", "alice", frozenset({"reader", "editor"}), frozenset({"support"}))
        self.rag_client = TestClient(self.create_rag(self.rag_settings, self.store, self.model))
        self.rag_client.__enter__()
        self.addCleanup(self.rag_client.__exit__, None, None, None)
        self.calls = []
        self.router_tool = "knowledge_qa"
        async def upstream(request):
            self.calls.append(request)
            if request.url.host == "rag.test":
                response = self.rag_client.request(request.method, request.url.raw_path.decode(), content=request.content, headers=dict(request.headers))
                return httpx.Response(response.status_code, content=response.content, headers={"Content-Type": "application/json"})
            if request.url.host == "usage.test":
                self.assertEqual("/v1/users/alice/usage", request.url.path)
                self.assertEqual("tenant-a", request.headers["X-Tenant-ID"])
                self.assertEqual("Bearer synthetic-internal-usage-secret", request.headers["Authorization"])
                return httpx.Response(200, json={"tenant_id": "tenant-a", "user_id": "alice", "month": "2026-09", "cleaning_count": 12, "cleaning_minutes": 360, "area_sqm": 720, "consumables": [{"name": "Filter", "remaining_percent": 65}], "source_version": "fixture-v1"})
            if request.url.host == "model.test":
                payload = json.loads(request.content)
                self.assertNotIn(self.token, request.content.decode())
                if "tools" in payload:
                    self.assertEqual("auto", payload["tool_choice"])
                    arguments = {"month": "2026-09"} if self.router_tool == "generate_usage_report" else {}
                    return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
                        "content": None, "tool_calls": [{"id": "cross-tool-1", "type": "function", "function": {
                            "name": self.router_tool, "arguments": json.dumps(arguments)}}]}}]})
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"recommendations": ["Inspect the filter according to the device manual."]})}}]})
            raise AssertionError("Unexpected upstream")
        settings = AgentSettings(identity_file=self.auth_file, rag_base_url="https://rag.test", reports_enabled=True, assistant_enabled=True,
                                 usage_api_base_url="https://usage.test", usage_api_token="synthetic-internal-usage-secret",
                                 model_base_url="https://model.test/v1", model_api_key="synthetic-model-secret", model_name="test-model")
        self.agent_client = TestClient(agent_app(settings, httpx.MockTransport(upstream)))
        self.agent_client.__enter__()
        self.addCleanup(self.agent_client.__exit__, None, None, None)

    def headers(self, key="cross-chat-0001", foreign=False):
        return {"Authorization": "Bearer " + (self.foreign_token if foreign else self.token), "Idempotency-Key": key}

    def publish(self):
        result = self.rag_client.post("/v1/documents", headers=self.headers("cross-upload-0001"), data={"knowledge_base_id": "support"}, files={"file": ("maintenance.txt", b"Clean the filter weekly.", "text/plain")})
        self.assertEqual(202, result.status_code, result.text)
        self.IndexWorker(self.rag_settings, self.store, self.model).run_once()
        return result.json()

    def test_upload_to_agent_answer_history_and_retry(self):
        self.publish()
        first = self.agent_client.post("/v1/chat", json={"question": "How to maintain the filter?"}, headers=self.headers())
        self.assertEqual(200, first.status_code, first.text)
        body = first.json()
        self.assertEqual(1, len(body["citations"]))
        retry = self.agent_client.post("/v1/chat", json={"question": "How to maintain the filter?"}, headers=self.headers())
        self.assertEqual(body["answer"], retry.json()["answer"])
        self.assertEqual(body["conversation_id"], retry.json()["conversation_id"])
        self.assertEqual(1, self.model.chat_calls)
        history = self.agent_client.get(f"/v1/conversations/{body['conversation_id']}/messages", headers=self.headers())
        self.assertEqual(200, history.status_code, history.text)
        self.assertEqual(["user", "assistant"], [m["role"] for m in history.json()["messages"]])

    def test_foreign_tenant_cannot_read_conversation_or_documents(self):
        self.publish()
        result = self.agent_client.post("/v1/chat", json={"question": "Maintenance?"}, headers=self.headers()).json()
        denied = self.agent_client.get(f"/v1/conversations/{result['conversation_id']}/messages", headers=self.headers(foreign=True))
        self.assertEqual(404, denied.status_code)
        foreign = self.agent_client.post("/v1/chat", json={"question": "Maintenance?"}, headers=self.headers(foreign=True))
        self.assertEqual(200, foreign.status_code, foreign.text)
        self.assertEqual([], foreign.json()["citations"])
        self.assertEqual(1, self.model.chat_calls)

    def test_deletion_invalidates_previous_answer_across_both_services(self):
        doc = self.publish()
        first = self.agent_client.post("/v1/chat", json={"question": "Maintenance?"}, headers=self.headers())
        self.assertEqual(200, first.status_code)
        deleted = self.rag_client.delete(f"/v1/documents/{doc['document_id']}", headers=self.headers())
        self.assertEqual(204, deleted.status_code)
        retry = self.agent_client.post("/v1/chat", json={"question": "Maintenance?"}, headers=self.headers())
        self.assertEqual(409, retry.status_code)

    def test_report_uses_trusted_business_identity_and_code_rendered_facts(self):
        report = self.agent_client.post("/v1/reports", json={"month": "2026-09"}, headers=self.headers())
        self.assertEqual(200, report.status_code, report.text)
        self.assertEqual(12, report.json()["facts"]["cleaning_count"])
        self.assertEqual("alice", report.json()["facts"]["user_id"])
        self.assertEqual(["usage.test", "model.test"], [r.url.host for r in self.calls])
        self.assertIn("12", report.json()["answer"])

    def test_identity_injection_rejected_before_any_external_call(self):
        report = self.agent_client.post("/v1/reports", json={"month": "2026-09", "user_id": "other"}, headers=self.headers())
        self.assertEqual(422, report.status_code)
        self.assertEqual([], self.calls)

    def test_assistant_knowledge_tool_uses_real_rag_retrieval_and_persists_original_question(self):
        self.publish()
        body = {"question": "How to maintain the filter?", "knowledge_base_id": "support"}
        response = self.agent_client.post("/v1/assistant", json=body, headers=self.headers("cross-assistant-001"))
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual("knowledge_qa", result["action"])
        self.assertEqual("Clean the filter weekly [1].", result["answer"])
        self.assertEqual(1, len(result["citations"]))
        self.assertEqual(1, self.model.chat_calls)
        self.assertEqual(["model.test", "rag.test"], [r.url.host for r in self.calls])
        history = self.agent_client.get(f"/v1/conversations/{result['conversation_id']}/messages", headers=self.headers())
        self.assertEqual(200, history.status_code, history.text)
        self.assertEqual(body["question"], history.json()["messages"][0]["content"])
        # The same key and original body reuse the real RAG response; only routing repeats.
        retry = self.agent_client.post("/v1/assistant", json=body, headers=self.headers("cross-assistant-001"))
        self.assertEqual(200, retry.status_code, retry.text)
        self.assertEqual(result["conversation_id"], retry.json()["conversation_id"])
        self.assertEqual(1, self.model.chat_calls)

    def test_assistant_report_tool_returns_original_validated_report(self):
        self.router_tool = "generate_usage_report"
        response = self.agent_client.post("/v1/assistant", json={"question": "Generate my 2026-09 usage report"},
                                          headers=self.headers("cross-assistant-002"))
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual("generate_usage_report", result["action"])
        self.assertEqual(12, result["report"]["facts"]["cleaning_count"])
        self.assertEqual("alice", result["report"]["facts"]["user_id"])
        self.assertEqual("tenant-a", result["report"]["facts"]["tenant_id"])
        self.assertEqual("2026-09", result["report"]["facts"]["month"])
        self.assertEqual(result["report"]["answer"], result["answer"])
        self.assertEqual(["model.test", "usage.test", "model.test"], [r.url.host for r in self.calls])
        self.assertNotIn("tools", json.loads(self.calls[-1].content))
        self.assertEqual(0, self.model.chat_calls)


if __name__ == "__main__":
    unittest.main()
