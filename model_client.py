"""Minimal non-streaming Chat Completions client; no fake fallback."""
import asyncio

import httpx

from config import Settings


class ModelError(Exception):
    def __init__(self, status: int, code: str, message: str, timeout_kind: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.timeout_kind = timeout_kind


async def generate_answer(client: httpx.AsyncClient, settings: Settings, message: str) -> str:
    headers = {}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    try:
        response = await client.post(
            settings.base_url + "/chat/completions",
            headers=headers,
            json={"model": settings.model,
                  "messages": [{"role": "user", "content": message}],
                  "stream": False},
        )
    except httpx.TimeoutException:
        raise ModelError(504, "model_timeout", "Model request timed out.", "http_phase") from None
    except httpx.RequestError:
        raise ModelError(503, "model_unavailable", "Cannot reach the model service.") from None
    if response.status_code == 429 or response.status_code >= 500:
        raise ModelError(503, "model_unavailable", "Model service is temporarily unavailable.")
    if response.status_code != 200:
        raise ModelError(502, "model_backend_error", "Model service rejected the request.")
    try:
        answer = response.json()["choices"][0]["message"]["content"]
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("Missing text")
    except (ValueError, KeyError, IndexError, TypeError):
        raise ModelError(502, "model_backend_error", "Model service returned no usable text.") from None
    return answer.strip()


async def answer_within_deadline(client: httpx.AsyncClient, settings: Settings, message: str) -> str:
    """Stop waiting for the model after MODEL_DEADLINE_SECONDS for the whole model step."""
    try:
        async with asyncio.timeout(settings.deadline_seconds):
            return await generate_answer(client, settings, message)
    except TimeoutError:
        raise ModelError(504, "model_timeout", "Model request timed out.", "model_deadline") from None
