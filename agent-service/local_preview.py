"""Start a loopback-only, disposable preview of both services with a fake model.

Run with the Python environment containing both projects' requirements plus
Streamlit and psycopg. No production credentials or existing project data are
read. `stop` removes the preview database and its temporary credentials.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import quote

import httpx


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT.parent / "rag-service"
STATE = ROOT / ".demo-state"
CONTAINER = "codex-rag-local-preview"
IMAGE = "registry-1.docker.io/pgvector/pgvector:0.8.1-pg17"


def free_port(preferred):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DemoModel:
    def embed(self, texts, deadline=None):
        return [[1.0, 0.0, 0.0] for _ in texts]

    def chat(self, messages, deadline=None):
        evidence = json.loads(messages[-1]["content"])["retrieved_evidence"]
        sample = evidence[0]["text"].replace("\n", " ")[:180]
        return "【离线演示，非真实模型回答】示例资料写道：" + sample + " [1]"


def serve(role):
    sys.path.insert(0, str(RAG))
    from enterprise.config import Settings
    from enterprise.db import Database
    from enterprise.service import IndexWorker
    from enterprise.store import Store

    settings = Settings.from_env()
    model = DemoModel()
    if role == "rag":
        import uvicorn
        from enterprise.api import create_app
        uvicorn.run(create_app(settings=settings, model=model), host="127.0.0.1", port=int(os.environ["PREVIEW_RAG_PORT"]), access_log=False)
    else:
        db = Database(settings)
        db.ready()
        worker = IndexWorker(settings, Store(db, settings), model)
        while True:
            if not worker.run_once():
                time.sleep(0.5)


def stop_processes(processes):
    for process in reversed(processes):
        pid = process if isinstance(process, int) else process.pid
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass


def wait_http(port, path="/health/live", headers=None, expected=200, timeout=25):
    end = time.monotonic() + timeout
    with httpx.Client(timeout=1, trust_env=False) as client:
        while time.monotonic() < end:
            try:
                response = client.get(f"http://127.0.0.1:{port}{path}", headers=headers)
                if response.status_code == expected:
                    return response
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
    raise RuntimeError(f"Preview service did not become ready on port {port}")


def spawn(args, cwd, env, name):
    stream = (STATE / (name + ".log")).open("ab")
    try:
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        return subprocess.Popen([sys.executable, *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=stream, stderr=subprocess.STDOUT, creationflags=flags,
                                start_new_session=os.name != "nt")
    finally:
        stream.close()


def start():
    if not RAG.is_dir():
        raise RuntimeError("The two projects must remain sibling directories")
    if STATE.exists() and any(STATE.iterdir()):
        raise RuntimeError("Preview state directory is not empty; use status or stop first")
    ports = {name: free_port(port) for name, port in [("postgres", 55439), ("rag", 8000),
             ("agent", 8001), ("rag_ui", 8501), ("agent_ui", 8502)]}
    if len(set(ports.values())) != len(ports):
        raise RuntimeError("Could not reserve distinct local preview ports")
    STATE.mkdir(mode=0o700, exist_ok=True)
    token = secrets.token_urlsafe(40)
    admin_password = secrets.token_urlsafe(40)
    app_password = secrets.token_urlsafe(40)
    principal = [{"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                  "tenant_id": "local-preview", "user_id": "demo", "roles": ["reader", "editor"],
                  "knowledge_base_ids": ["default"]}]
    for filename, value in [("token.txt", token), ("principals.json", json.dumps(principal)),
                             ("app-password.txt", app_password),
                             ("postgres.env", "POSTGRES_DB=rag_demo\nPOSTGRES_USER=rag_demo_admin\nPOSTGRES_PASSWORD=" + admin_password)]:
        target = STATE / filename
        target.write_text(value + "\n", encoding="utf-8")
        if os.name == "posix":
            target.chmod(0o600)
    admin_url = f"postgresql://rag_demo_admin:{quote(admin_password, safe='')}@127.0.0.1:{ports['postgres']}/rag_demo"
    app_url = f"postgresql://rag_app:{quote(app_password, safe='')}@127.0.0.1:{ports['postgres']}/rag_demo"
    (STATE / "admin-url.txt").write_text(admin_url, encoding="utf-8")
    if os.name == "posix":
        (STATE / "admin-url.txt").chmod(0o600)
    processes = []
    container_started = False
    try:
        result = subprocess.run(["docker", "run", "-d", "--rm", "--name", CONTAINER,
            "--env-file", str(STATE / "postgres.env"), "-p", f"127.0.0.1:{ports['postgres']}:5432", IMAGE],
            capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError("Could not start isolated preview PostgreSQL; check Docker Desktop and the local pgvector image")
        container_started = True
        import psycopg
        end = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(admin_url, connect_timeout=2):
                    break
            except psycopg.Error:
                if time.monotonic() >= end:
                    raise RuntimeError("Preview PostgreSQL startup timed out") from None
                time.sleep(0.3)
        env = dict(os.environ)
        for name in ("DATABASE_URL_FILE", "MODEL_API_KEY_FILE", "USAGE_API_TOKEN_FILE",
                     "DATABASE_ADMIN_URL_FILE", "DATABASE_APP_PASSWORD_FILE"):
            env.pop(name, None)
        env.update({"DATABASE_URL": app_url, "AUTH_PRINCIPALS_FILE": str(STATE / "principals.json"),
                    "MODEL_API_KEY": "offline-preview-unused", "MODEL_BASE_URL": "https://example.invalid/v1",
                    "EMBEDDING_DIMENSION": "3", "EMBEDDING_MODEL": "local-preview-embedding",
                    "DATABASE_ADMIN_URL_FILE": str(STATE / "admin-url.txt"),
                    "DATABASE_APP_PASSWORD_FILE": str(STATE / "app-password.txt"),
                    "PREVIEW_RAG_PORT": str(ports["rag"]), "RAG_BASE_URL": f"http://127.0.0.1:{ports['rag']}",
                    "ALLOW_INTERNAL_HTTP": "true", "REPORTS_ENABLED": "false", "ASSISTANT_ENABLED": "false",
                    "PYTHONDONTWRITEBYTECODE": "1"})
        result = subprocess.run([sys.executable, "-m", "enterprise.bootstrap"], cwd=RAG, env=env,
                                capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError("Preview database initialization failed")
        processes.append(spawn([str(Path(__file__).resolve()), "_serve", "rag"], RAG, env, "rag"))
        processes.append(spawn([str(Path(__file__).resolve()), "_serve", "worker"], RAG, env, "worker"))
        processes.append(spawn(["-m", "uvicorn", "service.main:app", "--host", "127.0.0.1",
                                "--port", str(ports["agent"]), "--no-access-log"], ROOT, env, "agent"))
        wait_http(ports["rag"], "/health/ready")
        headers = {"Authorization": "Bearer " + token}
        wait_http(ports["agent"], "/health/ready", headers=headers)
        with httpx.Client(timeout=5, trust_env=False) as client:
            sample = ("本机离线演示资料。智扫通设备的滤芯每周检查一次；发现堵塞时应停机清理，"
                      "完成检查后记录维护时间。此内容是合成测试数据，并非真实产品说明。")
            response = client.post(f"http://127.0.0.1:{ports['rag']}/v1/documents",
                headers={**headers, "Idempotency-Key": "preview-upload-0001"},
                data={"knowledge_base_id": "default"}, files={"file": ("local-preview.txt", sample.encode("utf-8"), "text/plain")})
            response.raise_for_status()
            version_id = response.json()["version_id"]
            end = time.monotonic() + 20
            while time.monotonic() < end:
                response = client.get(f"http://127.0.0.1:{ports['rag']}/v1/versions/{version_id}", headers=headers)
                response.raise_for_status()
                if response.json()["state"] == "ready":
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("Preview document indexing timed out")
            response = client.post(f"http://127.0.0.1:{ports['agent']}/v1/chat",
                headers={**headers, "Idempotency-Key": "preview-question-0001"},
                json={"question": "滤芯多久检查一次？", "knowledge_base_id": "default"})
            response.raise_for_status()
            if not response.json()["citations"]:
                raise RuntimeError("Preview question did not return a document citation")
        env.update({"AGENT_API_BASE_URL": f"http://127.0.0.1:{ports['agent']}",
                    "RAG_API_BASE_URL": f"http://127.0.0.1:{ports['rag']}"})
        for name, script, port in [("rag_ui", RAG / "app_file_uploader.py", ports["rag_ui"]),
                                   ("agent_ui", ROOT / "app.py", ports["agent_ui"])]:
            processes.append(spawn(["-m", "streamlit", "run", str(script), "--server.address", "127.0.0.1",
                                    "--server.port", str(port), "--server.headless", "true",
                                    "--browser.gatherUsageStats", "false"], script.parent, env, name))
            wait_http(port, "/", timeout=30)
        (STATE / "state.json").write_text(json.dumps({"ports": ports,
            "pids": [p.pid for p in processes], "container": CONTAINER}, indent=2), encoding="utf-8")
        print(f"RAG document UI: http://127.0.0.1:{ports['rag_ui']}")
        print(f"Agent chat UI:   http://127.0.0.1:{ports['agent_ui']}")
        print(f"Demo token file: {STATE / 'token.txt'}")
        print("Sample question: 滤芯多久检查一次？")
        print("This preview uses synthetic data and a fake model; reports are disabled.")
    except Exception:
        stop_processes(processes)
        if container_started:
            subprocess.run(["docker", "stop", CONTAINER], capture_output=True, check=False)
        raise


def stop():
    state_file = STATE / "state.json"
    if not state_file.is_file():
        raise RuntimeError("No active preview state was found")
    state = json.loads(state_file.read_text(encoding="utf-8"))
    if state["container"] != CONTAINER:
        raise RuntimeError("Unexpected preview container identity")
    stop_processes(state["pids"])
    subprocess.run(["docker", "stop", CONTAINER], capture_output=True, check=False)
    generated = ["state.json", "token.txt", "principals.json", "app-password.txt",
                 "postgres.env", "admin-url.txt", "rag.log", "worker.log", "agent.log",
                 "rag_ui.log", "agent_ui.log"]
    for name in generated:
        (STATE / name).unlink(missing_ok=True)
    if not any(STATE.iterdir()):
        STATE.rmdir()
    print("Local preview stopped; its disposable database and credentials removed.")


def status():
    state_file = STATE / "state.json"
    if not state_file.is_file():
        print("Local preview is not running")
        return
    ports = json.loads(state_file.read_text(encoding="utf-8"))["ports"]
    for name in ("rag", "agent", "rag_ui", "agent_ui"):
        try:
            wait_http(ports[name], "/health/live" if name in {"rag", "agent"} else "/", timeout=1)
            result = "reachable"
        except RuntimeError:
            result = "unavailable"
        print(f"{name}: http://127.0.0.1:{ports[name]} ({result})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status", "_serve"])
    parser.add_argument("role", nargs="?", choices=["rag", "worker"])
    args = parser.parse_args()
    if args.action == "_serve":
        serve(args.role)
    else:
        globals()[args.action]()
