"""One JSONL record per request, written to a file separate from the Uvicorn access log.

Read the log:  python request_logging.py logs/requests.jsonl
"""
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

LOGGER = logging.getLogger("aiguru.requests")
LOGGER.propagate = False
LOGGER.setLevel(logging.INFO)


def open_log(path: Path) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(handler)
    return handler


def close_log(handler: logging.Handler) -> None:
    LOGGER.removeHandler(handler)
    handler.close()


def write(record: dict) -> None:
    record = {"timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **record}
    LOGGER.info(json.dumps(record, ensure_ascii=False))


def outcome_for(request: Request, status: int) -> str:
    if request.state.outcome:
        return request.state.outcome
    if status == 422:
        return "validation_error"
    if request.url.path == "/health" and status == 200:
        return "health_ok"
    if status >= 500:
        return "internal_error"
    return "http_error"


async def log_requests(request: Request, call_next):
    """Middleware: attach X-Request-ID and X-Cache, then write the final log record."""
    request_id = uuid.uuid4().hex[:12]
    state = request.state
    # /chat starts as BYPASS: a 422 is produced before the route can mark MISS or HIT.
    state.cache_status = "BYPASS" if request.url.path == "/chat" else None
    state.outcome = None
    state.backend_calls = 0
    state.model_ms = None
    state.error_code = None
    state.timeout_kind = None
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        state.cache_status = None
        response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
    duration_ms = round((time.monotonic() - start) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    if state.cache_status:
        response.headers["X-Cache"] = state.cache_status
    write({"event": "request", "request_id": request_id, "method": request.method,
           "path": request.url.path, "status_code": response.status_code,
           "outcome": outcome_for(request, response.status_code),
           "cache_status": state.cache_status, "backend_calls": state.backend_calls,
           "duration_ms": duration_ms, "model_ms": state.model_ms,
           "error_code": state.error_code, "timeout_kind": state.timeout_kind})
    return response


def summarize(path: Path) -> None:
    """Print one short line per request; every line of the file must be valid JSON."""
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("event") != "request":
            continue
        model_ms = "-" if record["model_ms"] is None else record["model_ms"]
        print(f"{record['request_id']} {record['status_code']} {record['outcome']} "
              f"cache={record['cache_status'] or '-'} calls={record['backend_calls']} model_ms={model_ms}")


if __name__ == "__main__":
    summarize(Path(sys.argv[1] if len(sys.argv) > 1 else "logs/requests.jsonl"))
