"""Cache, deadline, concurrency and configuration tests (no real network)."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from cache import AnswerCache
from config import Settings
from main import create_app
from tests.test_api import make_settings, read_log


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def counting_backend(answer="Đáp án"):
    calls = []

    def backend(request):
        calls.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": f"{answer} {len(calls)}"}}]})
    return httpx.MockTransport(backend), calls


def test_miss_then_hit_calls_backend_once(tmp_path):
    transport, calls = counting_backend()
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, transport)) as client:
        first = client.post("/chat", json={"message": "Cache là gì?"})
        second = client.post("/chat", json={"message": "  Cache là gì?\n"})
    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "HIT")
    assert first.json() == second.json() == {"answer": "Đáp án 1"}
    assert len(calls) == 1
    hit = read_log(settings)[-1]
    assert (hit["outcome"], hit["backend_calls"], hit["model_ms"]) == ("cache_hit", 0, None)


@pytest.mark.parametrize("other", ["cache là gì?", "Cache  là gì?", "Cache la gi?", "Cache là gì"])
def test_different_text_is_a_different_key(tmp_path, other):
    transport, calls = counting_backend()
    with TestClient(create_app(make_settings(tmp_path), transport)) as client:
        client.post("/chat", json={"message": "Cache là gì?"})
        assert client.post("/chat", json={"message": other}).headers["x-cache"] == "MISS"
    assert len(calls) == 2


def test_model_or_base_url_change_does_not_reuse_answer(tmp_path):
    cache = AnswerCache(60, 10)
    cache.put(("https://a/v1", "model-a", "hỏi"), "A")
    assert cache.get(("https://a/v1", "model-b", "hỏi")) is None
    assert cache.get(("https://b/v1", "model-a", "hỏi")) is None
    assert cache.get(("https://a/v1", "model-a", "hỏi")) == "A"


def test_ttl_expiry_and_hit_does_not_extend(tmp_path):
    clock = FakeClock()
    transport, calls = counting_backend()
    with TestClient(create_app(make_settings(tmp_path, cache_ttl_seconds=10), transport, clock)) as client:
        assert client.post("/chat", json={"message": "q"}).headers["x-cache"] == "MISS"
        clock.now += 9.9
        assert client.post("/chat", json={"message": "q"}).headers["x-cache"] == "HIT"
        clock.now += 0.1
        third = client.post("/chat", json={"message": "q"})
    assert third.headers["x-cache"] == "MISS"
    assert third.json() == {"answer": "Đáp án 2"}
    assert len(calls) == 2


def test_fifo_limit_removes_oldest_entry():
    clock = FakeClock()
    cache = AnswerCache(60, 2, clock)
    cache.put(("u", "m", "1"), "one")
    cache.put(("u", "m", "2"), "two")
    assert cache.get(("u", "m", "1")) == "one"  # a read does not move the entry (not LRU)
    cache.put(("u", "m", "3"), "three")
    assert cache.get(("u", "m", "1")) is None
    assert cache.get(("u", "m", "2")) == "two"
    assert len(cache) == 2


def test_expired_entries_are_removed_before_fifo_eviction():
    clock = FakeClock()
    cache = AnswerCache(10, 2, clock)
    cache.put(("u", "m", "old"), "old")
    clock.now += 5
    cache.put(("u", "m", "kept"), "kept")
    clock.now += 6
    cache.put(("u", "m", "new"), "new")
    assert cache.get(("u", "m", "kept")) == "kept"
    assert cache.get(("u", "m", "new")) == "new"


def test_empty_answer_is_never_stored():
    cache = AnswerCache(60, 2)
    cache.put(("u", "m", "q"), "  ")
    assert len(cache) == 0


def test_new_app_starts_with_empty_cache(tmp_path):
    transport, calls = counting_backend()
    settings = make_settings(tmp_path)
    for _ in range(2):
        with TestClient(create_app(settings, transport)) as client:
            assert client.post("/chat", json={"message": "q"}).headers["x-cache"] == "MISS"
    assert len(calls) == 2


def slow_transport(seconds, calls=None):
    async def backend(request):
        if calls is not None:
            calls.append(request)
        await asyncio.sleep(seconds)
        return httpx.Response(200, json={"choices": [{"message": {"content": "muộn"}}]})
    return httpx.MockTransport(backend)


def test_deadline_returns_504_and_does_not_cache(tmp_path):
    calls = []
    settings = make_settings(tmp_path, timeout_seconds=10, deadline_seconds=0.2)
    with TestClient(create_app(settings, slow_transport(1, calls))) as client:
        first = client.post("/chat", json={"message": "q"})
        second = client.post("/chat", json={"message": "q"})
    for response in (first, second):
        assert response.status_code == 504
        assert response.headers["x-cache"] == "MISS"
        assert response.json() == {"detail": {"code": "model_timeout", "message": "Model request timed out."}}
    assert len(calls) == 2
    record = read_log(settings)[-1]
    assert (record["outcome"], record["timeout_kind"], record["backend_calls"]) == ("model_timeout", "model_deadline", 1)
    assert 150 <= record["model_ms"] < 900


def test_httpx_phase_timeout_has_its_own_kind(tmp_path):
    def backend(request):
        raise httpx.ReadTimeout("read", request=request)
    settings = make_settings(tmp_path, deadline_seconds=30)
    with TestClient(create_app(settings, httpx.MockTransport(backend))) as client:
        assert client.post("/chat", json={"message": "q"}).status_code == 504
    assert read_log(settings)[-1]["timeout_kind"] == "http_phase"


async def run_app(app, scenario):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://app") as client:
            return await scenario(client)


def test_health_answers_while_a_chat_is_waiting(tmp_path):
    app = create_app(make_settings(tmp_path, deadline_seconds=5), slow_transport(0.6))

    async def scenario(client):
        loop = asyncio.get_running_loop()
        chat = asyncio.create_task(client.post("/chat", json={"message": "q"}))
        await asyncio.sleep(0.1)
        start = loop.time()
        health = await client.get("/health")
        health_seconds = loop.time() - start
        assert not chat.done()
        return health, health_seconds, await chat

    health, health_seconds, chat = asyncio.run(run_app(app, scenario))
    assert health.status_code == 200 and health_seconds < 0.3
    assert chat.status_code == 200


def test_two_concurrent_misses_may_call_backend_twice(tmp_path):
    calls = []
    app = create_app(make_settings(tmp_path, deadline_seconds=5), slow_transport(0.3, calls))

    async def scenario(client):
        responses = await asyncio.gather(*[client.post("/chat", json={"message": "q"}) for _ in range(2)])
        after = await client.post("/chat", json={"message": "q"})
        return responses, after

    responses, after = asyncio.run(run_app(app, scenario))
    assert [r.status_code for r in responses] == [200, 200]
    assert [r.headers["x-cache"] for r in responses] == ["MISS", "MISS"]
    assert len(calls) == 2  # documented limit: no request coalescing
    assert after.headers["x-cache"] == "HIT"


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_TIMEOUT_SECONDS", "MODEL_DEADLINE_SECONDS",
                 "CACHE_TTL_SECONDS", "CACHE_MAX_ENTRIES", "REQUEST_LOG_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:18080/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")


@pytest.mark.parametrize("name,value", [("LLM_BASE_URL",""), ("LLM_BASE_URL","localhost"),
    ("LLM_BASE_URL","https://user:secret@host/v1"), ("LLM_BASE_URL","http://host:bad/v1"),
    ("LLM_BASE_URL","https://host/v1?key=secret"), ("LLM_BASE_URL","https://host/v1/chat/completions"),
    ("LLM_MODEL",""), ("LLM_MODEL","REPLACE_MODEL"), ("LLM_API_KEY","REPLACE_KEY"),
    ("LLM_TIMEOUT_SECONDS","nan"), ("LLM_TIMEOUT_SECONDS","0"), ("LLM_TIMEOUT_SECONDS","abc"),
    ("MODEL_DEADLINE_SECONDS","inf"), ("MODEL_DEADLINE_SECONDS","-1"), ("MODEL_DEADLINE_SECONDS","x"),
    ("CACHE_TTL_SECONDS","0"), ("CACHE_TTL_SECONDS","nan"),
    ("CACHE_MAX_ENTRIES","0"), ("CACHE_MAX_ENTRIES","1.5"), ("CACHE_MAX_ENTRIES","-3")])
def test_bad_settings_fail_at_startup(clean_env, monkeypatch, tmp_path, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setenv("REQUEST_LOG_FILE", str(tmp_path / "r.jsonl"))
    with pytest.raises(ValueError, match=name):
        with TestClient(create_app()):
            pass


def test_config_file_and_environment_priority(clean_env, monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("LLM_BASE_URL=https://openrouter.ai/api/v1\nLLM_MODEL=file-model\nLLM_API_KEY=file-secret\n"
                    "MODEL_DEADLINE_SECONDS=30\nCACHE_TTL_SECONDS=60\nCACHE_MAX_ENTRIES=100\n")
    monkeypatch.setenv("MODEL_DEADLINE_SECONDS", "1")
    settings = Settings.load(path)
    assert settings.model == "test-model"
    assert settings.base_url == "http://127.0.0.1:18080/v1"
    assert (settings.deadline_seconds, settings.cache_ttl_seconds, settings.cache_max_entries) == (1, 60, 100)
    assert settings.api_key == "file-secret"
    assert "file-secret" not in repr(settings)
    assert "file-secret" not in json.dumps(settings.public_summary())
    assert settings.request_log_file.name == "requests.jsonl"


def test_openrouter_requires_key(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        Settings.load(tmp_path / "absent.env")


def test_env_example_matches_settings(monkeypatch, tmp_path):
    from config import PROJECT_DIR
    example = (PROJECT_DIR / ".env.example").read_text()
    names = {line.split("=", 1)[0] for line in example.splitlines() if line and not line.startswith("#")}
    assert names == {"LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_TIMEOUT_SECONDS", "MODEL_DEADLINE_SECONDS",
                     "CACHE_TTL_SECONDS", "CACHE_MAX_ENTRIES", "REQUEST_LOG_FILE"}
    assert "MODEL_DEADLINE_SECONDS=30" in example
