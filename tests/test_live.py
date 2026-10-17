"""Optional live check with the real model from .env; skipped unless RUN_LIVE_TESTS=1."""
import os

import pytest
from fastapi.testclient import TestClient

from main import create_app

pytestmark = pytest.mark.live


@pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1",
                    reason="set RUN_LIVE_TESTS=1 and a valid .env to call the real model")
def test_live_model_miss_then_hit():
    with TestClient(create_app()) as client:
        first = client.post("/chat", json={"message": "Trả lời đúng một từ: xin chào."})
        second = client.post("/chat", json={"message": "Trả lời đúng một từ: xin chào."})
    assert first.status_code == 200 and first.json()["answer"].strip()
    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "HIT")
    assert first.json() == second.json()
