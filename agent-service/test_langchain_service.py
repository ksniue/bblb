"""Exercise real LangChain chains/tools against synthetic HTTP provider contracts."""
import asyncio
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

import httpx
from langsmith import get_tracing_context

from service.config import Settings
from service.main import create_app
from test_service import AUTH, TOKEN, USAGE, Fixture


def tool_response(name="knowledge_qa", arguments=None, *, count=1):
    return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
        "content": "Do not expose this unsupported model text",
        "tool_calls": [{"id": f"call-{i}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments or {})}} for i in range(count)],
    }}]})


class LangChainServiceTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()
        self.settings = replace(self.settings, assistant_enabled=True)
        self.headers = {**AUTH, "Idempotency-Key": "assistant-test-001"}

    def handler(self, name="knowledge_qa", arguments=None, *, count=1):
        def dispatch(request):
            payload = json.loads(request.content) if request.method == "POST" else {}
            if request.url.host == "model.test" and "tools" in payload:
                self.calls.append(request)
                self.assertIs(get_tracing_context()["enabled"], False)
                return tool_response(name, arguments, count=count)
            return self.success(request)
        return dispatch

    def invoke(self, client, question="滤芯如何维护？", **extra):
        return client.post("/v1/assistant", json={"question": question, **extra}, headers=self.headers)

    def test_knowledge_tool_passes_original_question_and_server_identity(self):
        with self.client(self.handler()) as client:
            response = self.invoke(client, knowledge_base_id="kb-a", conversation_id="conv-1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["action"], "knowledge_qa")
        self.assertEqual(response.json()["answer"], "知识回答")
        self.assertEqual(len(self.calls), 2)
        decision, rag = self.calls
        payload = json.loads(decision.content)
        self.assertEqual({item["function"]["name"] for item in payload["tools"]}, {"knowledge_qa", "generate_usage_report"})
        self.assertEqual(payload["tool_choice"], "auto")
        for private in [TOKEN, "internal-usage-token", "internal-model-key", "tenant-a", "user-a", "kb-a", "conv-1"]:
            self.assertNotIn(private, decision.content.decode())
        self.assertEqual(rag.headers["Authorization"], AUTH["Authorization"])
        self.assertEqual(rag.headers["Idempotency-Key"], self.headers["Idempotency-Key"])
        self.assertEqual(json.loads(rag.content), {"question": "滤芯如何维护？", "knowledge_base_id": "kb-a", "conversation_id": "conv-1"})
        self.assertNotIn("unsupported model text", response.text)
        self.assertNotIn("raw_tool_output", response.text)

    def test_report_tool_runs_chain_and_returns_facts_without_final_llm_rewrite(self):
        with self.client(self.handler("generate_usage_report", {"month": "2026-10"})) as client:
            response = self.invoke(client, "生成我的 2026-10 使用月报")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["action"], "generate_usage_report")
        self.assertEqual(result["answer"], result["report"]["answer"])
        self.assertIn("清洁次数：12", result["answer"])
        self.assertEqual(result["report"]["facts"], USAGE)
        self.assertEqual([call.url.host for call in self.calls], ["model.test", "usage.test", "model.test"])
        self.assertNotIn("tools", json.loads(self.calls[2].content))

    def test_unknown_tool_and_injected_identity_are_rejected_before_tool_execution(self):
        for name, arguments in [("delete_data", {}), ("knowledge_qa", {"question": "rewritten"}),
                                ("generate_usage_report", {"month": "2026-10", "user_id": "victim"}),
                                ("generate_usage_report", {"month": "2026-13"})]:
            self.calls.clear()
            with self.client(self.handler(name, arguments)) as client:
                response = self.invoke(client, "生成我的 2026-10 使用月报")
            self.assertEqual(response.status_code, 502, response.text)
            self.assertEqual(len(self.calls), 1)

    def test_multiple_tool_calls_rejected(self):
        with self.client(self.handler(count=2)) as client:
            response = self.invoke(client)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.calls), 1)

    def test_malformed_provider_objects_are_masked_as_502(self):
        messages = [None, [], {"tool_calls": [None]},
                    {"tool_calls": [{"id": "a", "type": "function", "function": []}]},
                    {"tool_calls": [{"id": "a", "type": "function", "function": {"name": "knowledge_qa", "arguments": "secret-non-json"}}]}]
        for message in messages:
            def handler(request):
                return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": message}]})
            with self.client(handler) as client:
                response = self.invoke(client)
            self.assertEqual(response.status_code, 502, response.text)
            self.assertNotIn("secret-non-json", response.text)

    def test_missing_ambiguous_or_invented_month_clarifies_without_accessing_records(self):
        for question in ["生成我上个月的月报", "生成我的 2026-09 月报", "生成 2026-09 和 2026-10 两份报告"]:
            self.calls.clear()
            with self.client(self.handler("generate_usage_report", {"month": "2026-10"})) as client:
                response = self.invoke(client, question)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["action"], "clarify")
            self.assertEqual(len(self.calls), 1)

    def test_model_free_text_is_replaced_with_fixed_clarification(self):
        def no_tool(request):
            self.calls.append(request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "fabricated report and policy"}}]})
        with self.client(no_tool) as client:
            response = self.invoke(client, "帮我看看")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "clarify")
        self.assertNotIn("fabricated", response.text)
        self.assertEqual(len(self.calls), 1)

    def test_wrong_kb_unknown_request_fields_or_missing_key_fail_before_model(self):
        with self.client(self.handler()) as client:
            self.assertEqual(self.invoke(client, knowledge_base_id="foreign").status_code, 403)
            self.assertEqual(self.invoke(client, user_id="foreign").status_code, 422)
            self.assertEqual(client.post("/v1/assistant", json={"question": "hi"}, headers=AUTH).status_code, 422)
            self.assertEqual(client.post("/v1/assistant", json={"question": "hi"}).status_code, 401)
        self.assertEqual(self.calls, [])

    def test_assistant_disabled_calls_no_dependency(self):
        with self.client(self.handler(), replace(self.settings, assistant_enabled=False)) as client:
            response = self.invoke(client)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.calls, [])

    def test_disabled_report_is_not_exposed_as_tool(self):
        with self.client(self.handler("generate_usage_report", {"month": "2026-10"}),
                         replace(self.settings, reports_enabled=False)) as client:
            response = self.invoke(client, "生成我的 2026-10 使用月报")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.calls), 1)
        payload = json.loads(self.calls[0].content)
        self.assertEqual([item["function"]["name"] for item in payload["tools"]], ["knowledge_qa"])

    def test_usage_identity_mismatch_fails_before_report_model(self):
        base = self.handler("generate_usage_report", {"month": "2026-10"})
        def handler(request):
            if request.url.host == "usage.test":
                self.calls.append(request)
                return httpx.Response(200, json={**USAGE, "user_id": "foreign"})
            return base(request)
        with self.client(handler) as client:
            response = self.invoke(client, "生成我的 2026-10 使用月报")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "usage_identity_mismatch")
        self.assertEqual(len(self.calls), 2)

    def test_incomplete_or_repaired_json_is_not_accepted_by_report_chain(self):
        for content in ['```json\n{"recommendations":["x"]}\n```', '{"recommendations":["x"]', '{"recommendations":["x"],"other":1}']:
            def handler(request):
                if request.url.host == "usage.test":
                    return httpx.Response(200, json=USAGE)
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
            with self.client(handler) as client:
                response = client.post("/v1/reports", json={"month": "2026-10"}, headers=AUTH)
            self.assertEqual(response.status_code, 502)

    def test_shared_rate_limit_stops_second_routing_model_call(self):
        with self.client(self.handler(), replace(self.settings, requests_per_minute=1)) as client:
            self.assertEqual(self.invoke(client).status_code, 200)
            self.assertEqual(self.invoke(client).status_code, 429)
        self.assertEqual(len(self.calls), 2)

    def test_model_key_loaded_when_only_assistant_is_enabled(self):
        env = {"AUTH_PRINCIPALS_FILE": str(self.settings.identity_file), "RAG_BASE_URL": "https://rag.test",
               "REPORTS_ENABLED": "false", "ASSISTANT_ENABLED": "true", "MODEL_BASE_URL": "https://model.test/v1",
               "MODEL_NAME": "fake", "MODEL_API_KEY": "test-key", "USAGE_API_TOKEN_FILE": "missing-file"}
        with patch.dict("os.environ", env, clear=True):
            configured = Settings.from_env()
        self.assertTrue(configured.assistant_enabled)
        self.assertEqual(configured.model_api_key, "test-key")
        self.assertEqual(configured.usage_api_token, "")


class AgentBudgetTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_fixture()

    async def test_router_cancellation_uses_same_total_budget(self):
        cancelled = asyncio.Event()
        async def slow(request):
            try:
                await asyncio.sleep(5)
            finally:
                cancelled.set()
        settings = replace(self.settings, assistant_enabled=True, request_timeout_seconds=1, upstream_timeout_seconds=1)
        app = create_app(settings, httpx.MockTransport(slow))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/v1/assistant", json={"question": "滤芯如何清洁"},
                                            headers={**AUTH, "Idempotency-Key": "timeout-test-001"})
        self.assertEqual(response.status_code, 504)
        self.assertTrue(cancelled.is_set())


if __name__ == "__main__":
    unittest.main()
