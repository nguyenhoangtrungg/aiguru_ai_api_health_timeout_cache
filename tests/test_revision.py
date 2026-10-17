"""Regression coverage for safe effective-config display and exact request selection."""
import json
import os
import sys
from pathlib import Path

import pytest

import request_logging
import show_config


@pytest.mark.parametrize("prefix", ["", "  ", "export "])
def test_config_display_redacts_key_and_uses_environment(tmp_path, monkeypatch, capsys, prefix):
    from config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_BASE_URL=https://model.test/v1\nLLM_MODEL=file-model\n"
        + prefix + "LLM_API_KEY=FAKE_REVIEW_TOKEN\nMODEL_DEADLINE_SECONDS=30\n"
    )
    for name in tuple(os.environ):
        if name.startswith(("LLM_", "MODEL_", "CACHE_", "REQUEST_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("MODEL_DEADLINE_SECONDS", "1")
    settings = Settings.load(env_file)
    monkeypatch.setattr(show_config.Settings, "load", lambda: settings)
    show_config.main()
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["api_key_set"] is True
    assert data["model_deadline_seconds"] == 1
    assert "FAKE_REVIEW_TOKEN" not in output
    assert "api_key" not in data


def test_header_selector_returns_current_timeout_not_old_one(tmp_path, monkeypatch, capsys):
    log = tmp_path / "requests.jsonl"
    old = {"event": "request", "request_id": "old", "timeout_kind": "http_phase"}
    new = {"event": "request", "request_id": "new", "timeout_kind": "model_deadline"}
    log.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n")
    headers = tmp_path / "response.headers"
    headers.write_bytes(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 504 Gateway Timeout\r\nx-request-id: new\r\n\r\n")
    monkeypatch.setattr(sys, "argv", ["request_logging.py", str(log), "--headers", str(headers)])
    request_logging.main()
    assert json.loads(capsys.readouterr().out) == new


def test_request_id_not_found_fails_instead_of_selecting_another_record(tmp_path):
    log = tmp_path / "requests.jsonl"
    log.write_text('{"event":"request","request_id":"old"}\n')
    with pytest.raises(SystemExit, match="No request record"):
        request_logging.show_request(log, "missing")


@pytest.mark.parametrize("content", ["HTTP/1.1 504\n", "X-Request-ID: a\nX-Request-ID: b\n"])
def test_header_selector_rejects_missing_or_ambiguous_id(tmp_path, monkeypatch, content):
    headers = tmp_path / "response.headers"
    headers.write_text(content)
    monkeypatch.setattr(sys, "argv", ["request_logging.py", "--headers", str(headers)])
    with pytest.raises(SystemExit) as error:
        request_logging.main()
    assert error.value.code == 2
