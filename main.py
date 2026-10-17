"""Run: python -m uvicorn main:app --host 127.0.0.1 --port 18054"""
from contextlib import asynccontextmanager
import logging
import time
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cache import AnswerCache
from config import Settings
from model_client import ModelError, answer_within_deadline
import request_logging


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(
        strict=True, min_length=1, max_length=2000,
        description="Text; strip outer whitespace before checking 1–2000 characters.",
        examples=["Giải thích cache trong một câu tiếng Việt."],
    )

    @field_validator("message", mode="before")
    @classmethod
    def strip_message(cls, value):
        return value.strip() if isinstance(value, str) else value


class ChatResponse(BaseModel):
    answer: str = Field(min_length=1, description="Text returned by the model API or by the cache.")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ErrorDetail(BaseModel):
    code: Literal["model_timeout", "model_unavailable", "model_backend_error"]
    message: str


class BackendErrorResponse(BaseModel):
    detail: ErrorDetail


HEADERS = {
    "X-Request-ID": {"description": "ID that matches the record in logs/requests.jsonl",
                     "schema": {"type": "string"}},
    "X-Cache": {"description": "HIT: answer from cache; MISS: model was called; BYPASS: invalid input",
                "schema": {"type": "string", "enum": ["HIT", "MISS", "BYPASS"]}},
}


def create_app(settings: Settings | None = None, transport=None, clock=time.monotonic) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings if settings is not None else Settings.load()
        app.state.cache = AnswerCache(app.state.settings.cache_ttl_seconds,
                                      app.state.settings.cache_max_entries, clock)
        handler = request_logging.open_log(app.state.settings.request_log_file)
        request_logging.write({"event": "startup", **app.state.settings.public_summary()})
        logging.getLogger("uvicorn.error").info(
            "AI Guru config: model=%s, deadline=%ss, cache_ttl=%ss, log=%s",
            app.state.settings.model, app.state.settings.deadline_seconds,
            app.state.settings.cache_ttl_seconds, app.state.settings.request_log_file.name)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(app.state.settings.timeout_seconds),
                follow_redirects=False, transport=transport,
            ) as client:
                app.state.model_client = client
                yield
        finally:
            request_logging.close_log(handler)

    app = FastAPI(
        title="AI Guru — Chat API with health, timeout and cache", version="1.1.0",
        lifespan=lifespan,
        description="Single-turn chat through an independent model API, with an in-memory answer cache.",
    )
    app.middleware("http")(request_logging.log_requests)

    @app.get("/health", response_model=HealthResponse, tags=["Health"],
             summary="Check application liveness, without calling the model")
    async def health() -> HealthResponse:
        return HealthResponse()

    @app.post("/chat", response_model=ChatResponse, tags=["Chat"], responses={
        200: {"headers": HEADERS},
        502: {"model": BackendErrorResponse, "description": "Backend rejected request or invalid response"},
        503: {"model": BackendErrorResponse, "description": "Backend unavailable"},
        504: {"model": BackendErrorResponse, "description": "Model step exceeded the timeout or deadline"},
    })
    async def chat(body: ChatRequest, request: Request) -> ChatResponse:
        state = request.state
        current = request.app.state.settings
        key = (current.base_url, current.model, body.message)
        cached = request.app.state.cache.get(key)
        if cached is not None:
            state.cache_status, state.outcome = "HIT", "cache_hit"
            return ChatResponse(answer=cached)
        state.cache_status, state.backend_calls = "MISS", 1
        start = time.monotonic()
        try:
            answer = await answer_within_deadline(request.app.state.model_client, current, body.message)
        except ModelError as exc:
            state.outcome = "model_timeout" if exc.code == "model_timeout" else "backend_error"
            state.error_code, state.timeout_kind = exc.code, exc.timeout_kind
            raise HTTPException(status_code=exc.status, detail={
                "code": exc.code, "message": exc.message,
            }) from None
        finally:
            state.model_ms = round((time.monotonic() - start) * 1000, 1)
        request.app.state.cache.put(key, answer)
        state.outcome = "model_success"
        return ChatResponse(answer=answer)

    return app


app = create_app()
