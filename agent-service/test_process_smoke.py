"""Real Uvicorn/worker process startup using the isolated integration database.

No LLM request is issued. The test provisions a least-privilege runtime role,
starts both HTTP services and the idle worker, and checks their actual network
and database wiring. Requires the same rag_test reset opt-in as other tests,
on a disposable dedicated instance: it also modifies the cluster's rag_app role.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import quote, urlsplit
from unittest.mock import patch

import httpx


URL = os.getenv("RAG_TEST_DATABASE_URL", "")
ENABLED = bool(URL and os.getenv("RAG_TEST_ALLOW_RESET") == "1")


@unittest.skipUnless(ENABLED, "requires explicitly opted-in dedicated rag_test database")
class ProcessSmoke(unittest.TestCase):
    def test_real_processes_start_with_restricted_database_credentials(self):
        if urlsplit(URL).path != "/rag_test":
            raise RuntimeError("Refusing non-test database")
        agent_root = Path(__file__).resolve().parents[1]
        rag_root = Path(os.getenv("RAG_PROJECT_PATH", str(agent_root.parent / "rag-service"))).resolve()
        sys.path.insert(0, str(rag_root))
        from enterprise.bootstrap import main as bootstrap
        from enterprise.config import Settings
        from enterprise.db import Database

        def free_port():
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return sock.getsockname()[1]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = secrets.token_urlsafe(40)
            password = secrets.token_urlsafe(40)
            auth_file = root / "principals.json"
            auth_file.write_text(json.dumps([{"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "tenant_id": "smoke", "user_id": "operator", "roles": ["reader", "editor"], "knowledge_base_ids": ["support"]}]), encoding="utf-8")
            admin_file, pass_file = root / "admin-url", root / "app-password"
            admin_file.write_text(URL, encoding="utf-8")
            pass_file.write_text(password, encoding="utf-8")
            parsed = urlsplit(URL)
            app_url = f"postgresql://rag_app:{quote(password, safe='')}@{parsed.hostname}:{parsed.port or 5432}/rag_test"
            settings = Settings(database_url=app_url, auth_file=auth_file, model_api_key="synthetic-unused-key", embedding_dimension=3)
            with patch("enterprise.bootstrap.Settings.from_env", return_value=settings), patch.dict(os.environ, {
                "DATABASE_ADMIN_URL_FILE": str(admin_file), "DATABASE_APP_PASSWORD_FILE": str(pass_file),
            }):
                bootstrap()
            with Database(Settings(database_url=URL, auth_file=auth_file, model_api_key="synthetic", embedding_dimension=3)).connection() as c:
                c.execute("TRUNCATE messages,chat_requests,conversations,chunks,versions,documents,rate_limits RESTART IDENTITY CASCADE")
            rag_port, agent_port = free_port(), free_port()
            while agent_port == rag_port:
                agent_port = free_port()
            env = dict(os.environ)
            for key in ["DATABASE_URL_FILE", "MODEL_API_KEY_FILE", "USAGE_API_TOKEN_FILE"]:
                env.pop(key, None)
            env.update({"DATABASE_URL": app_url, "AUTH_PRINCIPALS_FILE": str(auth_file), "MODEL_API_KEY": "synthetic-unused-key",
                        "EMBEDDING_DIMENSION": "3", "EMBEDDING_MODEL": "text-embedding-v4", "MODEL_BASE_URL": "https://example.invalid/v1",
                        "REPORTS_ENABLED": "false", "ASSISTANT_ENABLED": "false", "RAG_BASE_URL": f"http://127.0.0.1:{rag_port}",
                        "ALLOW_INTERNAL_HTTP": "true", "PYTHONDONTWRITEBYTECODE": "1"})
            processes = []
            logs = []
            log_paths = []
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            try:
                for name, cwd, args in [
                    ("rag", rag_root, ["-m", "uvicorn", "enterprise.api:app", "--host", "127.0.0.1", "--port", str(rag_port), "--no-access-log"]),
                    ("worker", rag_root, ["-m", "enterprise.worker"]),
                    ("agent", agent_root, ["-m", "uvicorn", "service.main:app", "--host", "127.0.0.1", "--port", str(agent_port), "--no-access-log"]),
                ]:
                    log_path = root / (name + ".log")
                    stream = log_path.open("wb")
                    logs.append(stream)
                    log_paths.append(log_path)
                    processes.append(subprocess.Popen([sys.executable, *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                                      stdout=stream, stderr=subprocess.STDOUT, **options))
                with httpx.Client(timeout=2, trust_env=False) as client:
                    for port in [rag_port, agent_port]:
                        deadline = time.monotonic() + 20
                        while True:
                            if any(p.poll() is not None for p in processes):
                                self.fail("A service exited before readiness; inspect local configuration")
                            try:
                                if client.get(f"http://127.0.0.1:{port}/health/live").status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            if time.monotonic() >= deadline:
                                self.fail("Service startup deadline exceeded")
                            time.sleep(.1)
                    base = f"http://127.0.0.1:{agent_port}"
                    self.assertEqual(401, client.get(base + "/health/ready").status_code)
                    headers = {"Authorization": "Bearer " + token}
                    ready = client.get(base + "/health/ready", headers=headers)
                    self.assertEqual(200, ready.status_code, ready.text)
                    self.assertFalse(ready.json()["reports_enabled"])
                    self.assertEqual(503, client.post(base + "/v1/reports", headers=headers, json={"month": "2026-09"}).status_code)
                    self.assertEqual(422, client.post(base + "/v1/reports", headers=headers, json={"month": "2026-09", "user_id": "other"}).status_code)
                    self.assertEqual(401, client.post(f"http://127.0.0.1:{rag_port}/v1/chat", headers={"Idempotency-Key": "smoke-question-01"}, json={"question": "Question"}).status_code)
                    self.assertTrue(all(p.poll() is None for p in processes))
            finally:
                for process in processes:
                    if process.poll() is None:
                        if os.name == "nt":
                            # Windows venv launchers may have a child interpreter.
                            # Stop the exact spawned tree before its parent exits.
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           timeout=10, check=False, **options)
                        else:
                            process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                for stream in logs:
                    stream.close()
                # Windows can release a terminated child interpreter's inherited
                # handles slightly after taskkill and the venv launcher exit.
                # Remove only the exact logs created above, within one 5s budget,
                # before TemporaryDirectory attempts its normal cleanup.
                cleanup_deadline = time.monotonic() + 5.0
                for log_path in log_paths:
                    if log_path.resolve().parent != root.resolve():
                        raise RuntimeError("Refusing to clean a log outside this test's temporary directory")
                    while True:
                        try:
                            log_path.unlink()
                            break
                        except FileNotFoundError:
                            break
                        except PermissionError as exc:
                            if os.name != "nt" or getattr(exc, "winerror", None) not in {32, 33}:
                                raise
                            remaining = cleanup_deadline - time.monotonic()
                            if remaining <= 0:
                                raise RuntimeError(f"Test log {log_path.name} remained locked after 5 seconds") from exc
                            time.sleep(min(0.05, remaining))


if __name__ == "__main__":
    unittest.main()
