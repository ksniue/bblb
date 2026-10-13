from pathlib import Path
import unittest
from fastapi.testclient import TestClient

from enterprise.api import create_app
from enterprise.auth import AccessDenied, Principal
from test_unit import FakeModel, FakeStore, PRINCIPAL, settings


class Auth:
    def authenticate(self, header):
        if header != "Bearer unit-test-token":
            raise AccessDenied()
        return PRINCIPAL


class ApiStore(FakeStore):
    def limit(self, principal):
        pass


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.store, self.model = ApiStore(), FakeModel()
        self.client = TestClient(create_app(settings=settings(), store=self.store, model=self.model, auth=Auth()))
        self.client.__enter__()
        self.headers = {"Authorization": "Bearer unit-test-token", "Idempotency-Key": "request-123"}

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_authentication_required_but_health_public(self):
        self.assertEqual(self.client.post("/v1/chat", json={"question": "x"}).status_code, 401)
        self.assertEqual(self.client.get("/health/ready").status_code, 200)

    def test_client_cannot_choose_identity_or_pass_empty_question(self):
        for body in ({"question": "", "knowledge_base_id": "kb"}, {"question": "x", "tenant_id": "foreign"}, {"question": "x", "user_id": "foreign"}):
            response = self.client.post("/v1/chat", json=body, headers=self.headers)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("foreign", response.text)
        self.assertEqual(self.model.chats, 0)

    def test_chat_requires_idempotency_and_returns_contract(self):
        self.assertEqual(self.client.post("/v1/chat", json={"question": "x"}, headers={"Authorization": "Bearer unit-test-token"}).status_code, 422)
        response = self.client.post("/v1/chat", json={"question": "x"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"answer", "citations", "conversation_id", "request_id"})

    def test_pdf_and_overlarge_body_rejected(self):
        response = self.client.post("/v1/documents", data={"knowledge_base_id": "kb"}, files={"file": ("a.pdf", b"%PDF-evil")}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/v1/chat", content="x" * 33000, headers=self.headers)
        self.assertEqual(response.status_code, 413)

    def test_internal_exception_is_sanitized(self):
        def broken(*args):
            raise RuntimeError("SECRET-CUSTOMER-DATA")
        self.store.claim_chat = broken
        response = self.client.post("/v1/chat", json={"question": "x"}, headers=self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("SECRET-CUSTOMER-DATA", response.text)


if __name__ == "__main__":
    unittest.main()
