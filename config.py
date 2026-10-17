"""Configuration is loaded at startup, never from a chat request."""
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import math
import os

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 60.0
    deadline_seconds: float = 30.0
    cache_ttl_seconds: float = 60.0
    cache_max_entries: int = 100
    request_log_file: Path = PROJECT_DIR / "logs" / "requests.jsonl"

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        path = env_file if env_file is not None else PROJECT_DIR / ".env"
        values = {**dotenv_values(path, interpolate=False), **os.environ}

        def value(name: str, default: str = "") -> str:
            return (values.get(name) or default).strip()

        def positive_number(name: str, default: str) -> float:
            try:
                number = float(value(name, default))
            except ValueError:
                raise ValueError(f"{name} must be a positive finite number") from None
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"{name} must be a positive finite number")
            return number

        base_url = value("LLM_BASE_URL").rstrip("/")
        try:
            url = urlsplit(base_url)
            port = url.port
            valid = (url.scheme in {"http", "https"} and url.hostname
                     and not url.username and not url.password
                     and not url.query and not url.fragment
                     and not any(c.isspace() for c in base_url))
            if port is not None and port == 0:
                valid = False
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("LLM_BASE_URL must be an HTTP(S) base URL without credentials/query/fragment")
        if url.path.endswith("/chat/completions"):
            raise ValueError("LLM_BASE_URL must not include /chat/completions")
        model = value("LLM_MODEL")
        if not model or model.startswith("REPLACE_"):
            raise ValueError("LLM_MODEL must contain a real model ID")
        key = value("LLM_API_KEY")
        if key.startswith("REPLACE_") or "\n" in key or "\r" in key:
            raise ValueError("LLM_API_KEY must contain a valid single-line credential")
        if url.hostname == "openrouter.ai" and not key:
            raise ValueError("LLM_API_KEY is required for OpenRouter")
        timeout = positive_number("LLM_TIMEOUT_SECONDS", "60")
        deadline = positive_number("MODEL_DEADLINE_SECONDS", "30")
        ttl = positive_number("CACHE_TTL_SECONDS", "60")
        raw_entries = value("CACHE_MAX_ENTRIES", "100")
        if not raw_entries.isdigit() or int(raw_entries) <= 0:
            raise ValueError("CACHE_MAX_ENTRIES must be a positive integer")
        log_file = Path(value("REQUEST_LOG_FILE", "logs/requests.jsonl"))
        if not log_file.is_absolute():
            log_file = PROJECT_DIR / log_file
        return cls(base_url, model, key, timeout, deadline, ttl, int(raw_entries), log_file)

    def public_summary(self) -> dict:
        """Settings that are safe to print: never the API key."""
        return {"base_url": self.base_url, "model": self.model,
                "api_key_set": bool(self.api_key),
                "llm_timeout_seconds": self.timeout_seconds,
                "model_deadline_seconds": self.deadline_seconds,
                "cache_ttl_seconds": self.cache_ttl_seconds,
                "cache_max_entries": self.cache_max_entries}
