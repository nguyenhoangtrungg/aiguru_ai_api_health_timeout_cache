"""One JSONL record per request, written to a file separate from the Uvicorn access log.

Read the log:  python request_logging.py logs/requests.jsonl
"""
import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
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
    if status >= 400:
        return "http_error"
    return "other"  # e.g. /docs and /openapi.json


async def log_requests(request: Request, call_next):
    """Middleware: attach X-Request-ID and X-Cache, then write the final log record."""
    request_id = uuid.uuid4().hex[:12]
    state = request.state
    # BYPASS means the route has not looked up the cache (e.g. 422 or 405).
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
        logging.getLogger("uvicorn.error").exception(
            "Unhandled request error request_id=%s", request_id)
        state.outcome = "internal_error"
        state.error_code = "internal_error"
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


def show_request(path: Path, request_id: str) -> None:
    """Find the exact request, even when older runs are present in the same file."""
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("event") == "request" and record.get("request_id") == request_id:
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return
    raise SystemExit(f"No request record for ID {request_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read request JSONL logs")
    parser.add_argument("path", nargs="?", type=Path, default=Path("logs/requests.jsonl"))
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--request-id", help="print the JSON record for this exact ID")
    selector.add_argument("--headers", type=Path, help="read X-Request-ID from a curl -D file")
    args = parser.parse_args()
    request_id = args.request_id
    if args.headers:
        ids = []
        for line in args.headers.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition(":")
            if sep and name.lower() == "x-request-id":
                ids.append(value.strip())
        if len(ids) != 1 or not ids[0]:
            parser.error("headers file must contain exactly one non-empty X-Request-ID")
        request_id = ids[0]
    if request_id:
        show_request(args.path, request_id)
    else:
        summarize(args.path)


if __name__ == "__main__":
    main()
