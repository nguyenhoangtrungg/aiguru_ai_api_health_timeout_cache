"""Real processes: Uvicorn + demo/slow_backend.py over HTTP. No real model."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest

PROJECT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.integration


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(port, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(process.stdout.read())
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} did not open")


@pytest.fixture
def stack(tmp_path):
    processes = []

    def start(delay, deadline):
        backend_port, app_port = free_port(), free_port()
        backend = subprocess.Popen([sys.executable, "demo/slow_backend.py", "--port", str(backend_port),
                                    "--delay", str(delay)], cwd=PROJECT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        processes.append(backend)
        wait_for(backend_port, backend)
        env = dict(os.environ, LLM_BASE_URL=f"http://127.0.0.1:{backend_port}/v1", LLM_MODEL="demo-backend",
                   LLM_API_KEY="", LLM_TIMEOUT_SECONDS="10", MODEL_DEADLINE_SECONDS=str(deadline),
                   CACHE_TTL_SECONDS="60", CACHE_MAX_ENTRIES="100",
                   REQUEST_LOG_FILE=str(tmp_path / "requests.jsonl"))
        app = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                                "--port", str(app_port)], cwd=PROJECT, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
        processes.append(app)
        wait_for(app_port, app)
        return f"http://127.0.0.1:{app_port}", f"http://127.0.0.1:{backend_port}", tmp_path / "requests.jsonl"

    yield start
    for process in processes:
        process.terminate()
        process.wait(timeout=10)


def test_real_http_timeout_after_deadline(stack):
    app, backend, log = stack(delay=2, deadline=0.5)
    start = time.monotonic()
    response = httpx.post(app + "/chat", json={"message": "Timeout thật"}, timeout=10)
    elapsed = time.monotonic() - start
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "model_timeout"
    assert 0.4 <= elapsed < 1.8
    record = json.loads(log.read_text().splitlines()[-1])
    assert (record["outcome"], record["timeout_kind"]) == ("model_timeout", "model_deadline")
    assert httpx.get(app + "/health").status_code == 200


def test_real_http_cache_hit_is_counted_by_backend(stack):
    app, backend, log = stack(delay=0, deadline=5)
    first = httpx.post(app + "/chat", json={"message": "Cache thật"}, timeout=10)
    second = httpx.post(app + "/chat", json={"message": "Cache thật"}, timeout=10)
    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "HIT")
    assert first.json() == second.json()
    assert httpx.get(backend + "/stats").json() == {"requests": 1}
    ids = [json.loads(line)["request_id"] for line in log.read_text().splitlines()[1:]]
    assert ids == [first.headers["x-request-id"], second.headers["x-request-id"]]
