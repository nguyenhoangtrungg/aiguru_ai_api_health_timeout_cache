"""Contract tests kept from #53 plus the new headers and log records."""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from config import Settings
from main import create_app


def make_settings(tmp_path: Path, **changes) -> Settings:
    values = dict(base_url="https://model.test/v1", model="test-model", api_key="secret-test-key",
                  timeout_seconds=2, deadline_seconds=2, cache_ttl_seconds=60, cache_max_entries=100,
                  request_log_file=tmp_path / "requests.jsonl")
    values.update(changes)
    return Settings(**values)


def read_log(settings: Settings, event="request"):
    lines = settings.request_log_file.read_text(encoding="utf-8").splitlines()
    return [r for r in map(json.loads, lines) if r["event"] == event]


@pytest.fixture
def api(tmp_path):
    calls = []

    def backend(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "  Trả lời từ backend.  "}}]})

    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, httpx.MockTransport(backend))) as client:
        yield client, calls, settings


def test_health_does_not_call_model_or_touch_cache(api):
    client, calls, settings = api
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "x-cache" not in response.headers
    assert len(client.app.state.cache) == 0
    assert not calls
    record = read_log(settings)[-1]
    assert record["outcome"] == "health_ok" and record["request_id"] == response.headers["x-request-id"]


def test_chat_request_and_response(api):
    client, calls, settings = api
    response = client.post("/chat", json={"message": "  Xin  chào\n"})
    assert response.status_code == 200
    assert response.json() == {"answer": "Trả lời từ backend."}
    assert response.headers["x-cache"] == "MISS"
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://model.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer secret-test-key"
    assert json.loads(request.content) == {
        "model": "test-model", "messages": [{"role": "user", "content": "Xin  chào"}], "stream": False,
    }
    assert request.extensions["timeout"] == dict(connect=2, read=2, write=2, pool=2)
    record = read_log(settings)[-1]
    assert (record["outcome"], record["cache_status"], record["backend_calls"]) == ("model_success", "MISS", 1)
    assert record["model_ms"] >= 0 and record["duration_ms"] >= 0


@pytest.mark.parametrize("body", [{}, {"message": None}, {"message": 7}, {"message": True},
    {"message": []}, {"message": {}}, {"message": ""}, {"message": " \t\n "},
    {"message": "a" * 2001}, {"message": "ok", "extra": 1}, [], "text"])
def test_invalid_body_never_calls_backend(api, body):
    client, calls, settings = api
    response = client.post("/chat", json=body)
    assert response.status_code == 422
    assert response.headers["x-cache"] == "BYPASS"
    assert not calls
    record = read_log(settings)[-1]
    assert (record["outcome"], record["backend_calls"]) == ("validation_error", 0)


@pytest.mark.parametrize("body", [b"", b'{"message":'])
def test_missing_or_malformed_json(api, body):
    client, calls, _ = api
    assert client.post("/chat", content=body, headers={"Content-Type": "application/json"}).status_code == 422
    assert not calls


@pytest.mark.parametrize("length", [1, 2000])
def test_length_after_strip(api, length):
    client, calls, _ = api
    assert client.post("/chat", json={"message": "  " + "á" * length + "\n"}).status_code == 200
    assert len(json.loads(calls[0].content)["messages"][0]["content"]) == length


def test_routes_and_openapi(api):
    client, calls, settings = api
    assert client.get("/chat").status_code == 405
    assert client.get("/missing").status_code == 404
    assert [r["outcome"] for r in read_log(settings)] == ["http_error", "http_error"]
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    post = schema["paths"]["/chat"]["post"]
    assert post["requestBody"]["required"] is True
    assert set(post["responses"]) == {"200", "422", "502", "503", "504"}
    assert set(post["responses"]["200"]["headers"]) == {"X-Request-ID", "X-Cache"}
    models = schema["components"]["schemas"]
    request = models["ChatRequest"]
    assert request["required"] == ["message"]
    assert request["additionalProperties"] is False
    assert request["properties"]["message"]["minLength"] == 1
    assert request["properties"]["message"]["maxLength"] == 2000
    assert models["ChatResponse"]["required"] == ["answer"]
    assert not calls


@pytest.mark.parametrize("status,expected,code", [(401,502,"model_backend_error"),
    (403,502,"model_backend_error"), (404,502,"model_backend_error"),
    (400,502,"model_backend_error"), (302,502,"model_backend_error"),
    (429,503,"model_unavailable"), (500,503,"model_unavailable"), (503,503,"model_unavailable")])
def test_backend_http_errors(tmp_path, status, expected, code):
    def backend(request):
        return httpx.Response(status, text="secret-test-key PRIVATE BACKEND ERROR", headers={"Location": "https://other.test"})
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, httpx.MockTransport(backend))) as client:
        response = client.post("/chat", json={"message": "test"})
        assert response.status_code == expected
        assert response.headers["x-cache"] == "MISS"
        assert response.json()["detail"]["code"] == code
        assert "secret-test-key" not in response.text
        assert "PRIVATE" not in response.text
        assert len(client.app.state.cache) == 0
    record = read_log(settings)[-1]
    assert (record["outcome"], record["error_code"], record["backend_calls"]) == ("backend_error", code, 1)
    assert "secret-test-key" not in settings.request_log_file.read_text()


@pytest.mark.parametrize("error,expected", [(httpx.ReadTimeout,504), (httpx.ConnectTimeout,504),
    (httpx.ConnectError,503), (httpx.RemoteProtocolError,503)])
def test_backend_network_errors_and_health(tmp_path, error, expected):
    def backend(request):
        raise error("secret-test-key", request=request)
    with TestClient(create_app(make_settings(tmp_path), httpx.MockTransport(backend))) as client:
        assert client.get("/health").status_code == 200
        response = client.post("/chat", json={"message": "test"})
        assert response.status_code == expected
        assert "secret-test-key" not in response.text


@pytest.mark.parametrize("payload", [None, {}, [], {"choices": []}, {"choices": [None]},
    {"choices": [{"message": {"content": None}}]}, {"choices": [{"message": {"content": " "}}]},
    {"choices": [{"message": {"content": []}}]}, {"error": {"message": "provider error in 200"}}])
def test_invalid_backend_payload_is_not_cached(tmp_path, payload):
    backend = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    with TestClient(create_app(make_settings(tmp_path), backend)) as client:
        assert client.post("/chat", json={"message": "test"}).status_code == 502
        assert len(client.app.state.cache) == 0


def test_backend_invalid_json(tmp_path):
    backend = httpx.MockTransport(lambda r: httpx.Response(200, content=b"not JSON"))
    with TestClient(create_app(make_settings(tmp_path), backend)) as client:
        assert client.post("/chat", json={"message": "test"}).status_code == 502


def test_self_host_no_key(tmp_path):
    def backend(request):
        assert "authorization" not in request.headers
        assert str(request.url) == "http://127.0.0.1:18080/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    settings = make_settings(tmp_path, base_url="http://127.0.0.1:18080/v1", model="local", api_key="")
    with TestClient(create_app(settings, httpx.MockTransport(backend))) as client:
        assert client.post("/chat", json={"message": "test"}).status_code == 200


def test_unexpected_error_is_logged_as_500(tmp_path, monkeypatch):
    import main

    async def broken(*_):
        raise RuntimeError("bug secret-test-key")
    monkeypatch.setattr(main, "answer_within_deadline", broken)
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.post("/chat", json={"message": "test"})
    assert response.status_code == 500
    assert "secret-test-key" not in response.text
    record = read_log(settings)[-1]
    assert record["outcome"] == "internal_error"
    assert record["request_id"] == response.headers["x-request-id"]


def test_log_lines_are_json_and_safe(api):
    client, _, settings = api
    client.get("/health")
    client.post("/chat", json={"message": "Câu hỏi riêng"})
    client.post("/chat", json={"message": "Câu hỏi riêng"})
    client.post("/chat", json={"message": " "})
    text = settings.request_log_file.read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines()]
    assert records[0]["event"] == "startup" and records[0]["api_key_set"] is True
    assert [r["outcome"] for r in records[1:]] == ["health_ok", "model_success", "cache_hit", "validation_error"]
    for secret in ("secret-test-key", "Câu hỏi riêng", "Trả lời từ backend"):
        assert secret not in text
