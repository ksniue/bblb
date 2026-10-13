import asyncio
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from service.auth import IdentityStore
from service.config import Settings
from service.main import create_app

TOKEN = "test-access-token-with-at-least-32-characters"
AUTH = {"Authorization": "Bearer " + TOKEN}
USAGE = {"tenant_id": "tenant-a", "user_id": "user-a", "month": "2026-10",
         "cleaning_count": 12, "cleaning_minutes": 360, "area_sqm": 800,
         "consumables": [{"name": "滤芯", "remaining_percent": 35}], "source_version": "fixture-v1"}


class Fixture:
    def setup_fixture(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        identity = Path(self.temp.name) / "principals.json"
        identity.write_text(json.dumps([{"token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
            "tenant_id": "tenant-a", "user_id": "user-a", "roles": ["reader"], "knowledge_base_ids": ["kb-a"]}]), encoding="utf-8")
        self.settings = Settings(identity_file=identity, rag_base_url="https://rag.test", reports_enabled=True,
            usage_api_base_url="https://usage.test", usage_api_token="internal-usage-token",
            model_base_url="https://model.test/v1", model_api_key="internal-model-key", model_name="fake-model")
        self.calls = []

    def success(self, request):
        self.calls.append(request)
        if request.url.host == "usage.test":
            return httpx.Response(200, json=USAGE)
        if request.url.host == "model.test":
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"recommendations": ["定期按说明书清洁滤芯。"]}, ensure_ascii=False)}}]})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"conversation_id": "conv-1", "messages": [], "limit": 200})
        if request.url.path.endswith("/health/ready"):
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(200, json={"answer": "知识回答", "citations": [], "conversation_id": "conv-1",
            "request_id": "upstream-id", "raw_tool_output": "must-not-leak"})

    def client(self, handler=None, settings=None):
        return TestClient(create_app(settings or self.settings, httpx.MockTransport(handler or self.success)))


class ServiceTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()

    def test_anonymous_never_calls_dependencies(self):
        with self.client() as client:
            self.assertEqual(client.post("/v1/reports", json={"month": "2026-10"}).status_code, 401)
        self.assertEqual(self.calls, [])

    def test_identity_and_month_cannot_be_overridden(self):
        with self.client() as client:
            for body in [{"month": "2026-10", "user_id": "user-b"}, {"month": "2026-13"}, {"month": "2026-1"}]:
                self.assertEqual(client.post("/v1/reports", json=body, headers=AUTH).status_code, 422)
        self.assertEqual(self.calls, [])

    def test_report_uses_trusted_identity_and_code_rendered_facts(self):
        with self.client() as client:
            result = client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0].url.path, "/v1/users/user-a/usage")
        self.assertEqual(self.calls[0].headers["X-Tenant-ID"], "tenant-a")
        self.assertEqual(self.calls[0].headers["Authorization"], "Bearer internal-usage-token")
        prompt = self.calls[1].content.decode()
        for secret in [TOKEN, "internal-usage-token", "internal-model-key", "user-a", "tenant-a"]:
            self.assertNotIn(secret, prompt)
        self.assertIn("清洁次数：12", result.json()["answer"])
        self.assertNotIn("tool_calls", result.text)

    def test_wrong_usage_identity_prevents_model_call(self):
        def wrong(request):
            self.calls.append(request)
            return httpx.Response(200, json={**USAGE, "user_id": "other-user"})
        with self.client(wrong) as client:
            result = client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()["error"]["code"], "usage_identity_mismatch")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("other-user", result.text)

    def test_missing_record_and_dependency_errors_are_distinct(self):
        for code, expected in [(404, 404), (429, 503), (500, 502)]:
            with self.client(lambda request: httpx.Response(code, text="private upstream error")) as client:
                result = client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
                self.assertEqual(result.status_code, expected)
                self.assertNotIn("private", result.text)

    def test_timeout_is_masked(self):
        def fail(request):
            raise httpx.ReadTimeout("internal secret", request=request)
        with self.client(fail) as client:
            result = client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
        self.assertEqual(result.status_code, 504)
        self.assertNotIn("internal secret", result.text)

    def test_redirect_is_not_followed(self):
        def redirect(request):
            self.calls.append(request)
            return httpx.Response(307, headers={"location": "https://attacker.test/steal"})
        with self.client(redirect) as client:
            result = client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(len(self.calls), 1)

    def test_invalid_usage_numbers_do_not_reach_model(self):
        for patch in [{"cleaning_count": -1}, {"cleaning_count": "12"}, {"area_sqm": "NaN"}]:
            with self.client(lambda request: httpx.Response(200, json={**USAGE, **patch})) as client:
                result = client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
                self.assertEqual(result.status_code, 502)

    def test_invalid_or_truncated_model_response_fails(self):
        for reason, content in [("length", '{"recommendations":["x"]}'), ("stop", "not JSON")]:
            def handler(request):
                if request.url.host == "usage.test":
                    return httpx.Response(200, json=USAGE)
                return httpx.Response(200, json={"choices": [{"finish_reason": reason, "message": {"content": content}}]})
            with self.client(handler) as client:
                self.assertEqual(client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH).status_code, 502)

    def test_reports_disabled_is_explicit(self):
        with self.client(settings=replace(self.settings, reports_enabled=False)) as client:
            result = client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.calls, [])

    def test_chat_auth_key_and_only_final_fields(self):
        headers = {"Authorization": "bearer " + TOKEN, "Idempotency-Key": "request-123"}
        with self.client() as client:
            response = client.post("/v1/chat", json={"question": "说明维护方式", "knowledge_base_id": "kb-a"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {"answer", "citations", "conversation_id", "request_id"})
        self.assertEqual(self.calls[0].headers["Authorization"], AUTH["Authorization"])
        self.assertEqual(self.calls[0].headers["Idempotency-Key"], "request-123")

    def test_wrong_kb_and_missing_key_never_forward(self):
        with self.client() as client:
            self.assertEqual(client.post("/v1/chat", json={"question": "hi"}, headers=AUTH).status_code, 422)
            self.assertEqual(client.post("/v1/chat", json={"question": "hi", "knowledge_base_id": "other"},
                headers={**AUTH, "Idempotency-Key": "request-123"}).status_code, 403)
        self.assertEqual(self.calls, [])

    def test_history_never_returns_tool_messages(self):
        def bad(request):
            return httpx.Response(200, json={"conversation_id": "conv-1", "limit": 200,
                "messages": [{"role": "tool", "content": "private", "citations": [], "created_at": "2026-10-08"}]})
        with self.client(bad) as client:
            response = client.get("/v1/conversations/conv-1/messages", headers=AUTH)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private", response.text)

    def test_large_body_is_rejected_before_dependencies(self):
        with self.client() as client:
            result = client.post("/v1/chat", json={"question": "a" * 40000}, headers=AUTH)
        self.assertEqual(result.status_code, 413)
        self.assertEqual(self.calls, [])

    def test_large_upstream_is_bounded(self):
        with self.client(lambda request: httpx.Response(200, content=b"x" * (1024 * 1024 + 1))) as client:
            result = client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
        self.assertEqual(result.status_code, 502)

    def test_configuration_fails_closed(self):
        with self.assertRaises(ValueError):
            replace(self.settings, model_api_key="").validate()
        with self.assertRaises(ValueError):
            replace(self.settings, rag_base_url="http://rag.test").validate()
        with self.assertRaises(ValueError):
            replace(self.settings, rag_base_url="https://secret:password@rag.test").validate()

    def test_duplicate_identity_hash_rejected(self):
        path = self.settings.identity_file
        entries = json.loads(path.read_text())
        path.write_text(json.dumps(entries + entries), encoding="utf-8")
        with self.assertRaises(ValueError):
            IdentityStore(path)

    def test_per_user_budget_limits_billable_requests(self):
        with self.client(settings=replace(self.settings, requests_per_minute=1)) as client:
            first = client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
            second = client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(len(self.calls), 2)

    def test_internal_http_permission_does_not_allow_model_http(self):
        with self.assertRaises(ValueError):
            replace(self.settings, allow_internal_http=True, model_base_url="http://model.test/v1").validate()

    def test_disabled_reports_do_not_read_missing_unused_secret_files(self):
        env = {"AUTH_PRINCIPALS_FILE": str(self.settings.identity_file), "RAG_BASE_URL": "https://rag.test",
               "REPORTS_ENABLED": "false", "MODEL_API_KEY_FILE": "missing-file", "USAGE_API_TOKEN_FILE": "missing-file"}
        with patch.dict("os.environ", env, clear=True):
            self.assertFalse(Settings.from_env().reports_enabled)

    def test_reports_default_to_disabled_when_flag_is_omitted(self):
        env = {"AUTH_PRINCIPALS_FILE": str(self.settings.identity_file), "RAG_BASE_URL": "https://rag.test",
               "MODEL_API_KEY_FILE": "missing-file", "USAGE_API_TOKEN_FILE": "missing-file"}
        with patch.dict("os.environ", env, clear=True):
            configured = Settings.from_env()
        self.assertFalse(configured.reports_enabled)
        self.assertFalse(Settings(identity_file=self.settings.identity_file, rag_base_url="https://rag.test").reports_enabled)
        with self.client(settings=configured) as client:
            response = client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.calls, [])

    def test_boolean_typo_fails_startup(self):
        with patch.dict("os.environ", {"AUTH_PRINCIPALS_FILE": str(self.settings.identity_file), "REPORTS_ENABLED": "flase"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()


class AsyncBudgetTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_fixture()

    async def test_total_deadline_cancels_upstream(self):
        cancelled = asyncio.Event()
        async def slow(request):
            try:
                await asyncio.sleep(5)
            finally:
                cancelled.set()
        settings = replace(self.settings, request_timeout_seconds=1, upstream_timeout_seconds=1)
        app = create_app(settings, httpx.MockTransport(slow))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
                self.assertEqual(response.status_code, 504)
                self.assertTrue(cancelled.is_set())

    async def test_capacity_returns_429_without_extra_call(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(request):
            entered.set()
            await release.wait()
            return self.success(request)
        app = create_app(replace(self.settings, max_concurrent_requests=1), httpx.MockTransport(slow))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH))
                await asyncio.wait_for(entered.wait(), 2)
                second = await client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
                self.assertEqual(second.status_code, 429)
                release.set()
                self.assertEqual((await first).status_code, 200)


if __name__ == "__main__":
    unittest.main()
