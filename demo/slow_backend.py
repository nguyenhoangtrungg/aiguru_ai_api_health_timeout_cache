"""Test backend that imitates Chat Completions with a configurable delay. No real model.

Run: python demo/slow_backend.py --port 18055 --delay 3
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

parser = argparse.ArgumentParser(description="Fake Chat Completions backend for the timeout/cache demo")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=18055)
parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait before answering")
args = parser.parse_args()

counter = 0
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/stats":
            self.send_json(200, {"requests": counter})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        global counter
        if self.path != "/v1/chat/completions":
            self.send_json(404, {"error": "not found"})
            return
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        with lock:
            counter += 1
            number = counter
        print(f"[backend] request #{number} model={payload.get('model')} delay={args.delay}s", flush=True)
        time.sleep(args.delay)
        answer = f"Câu trả lời thử nghiệm số {number} từ backend giả lập."
        try:
            self.send_json(200, {"choices": [{"message": {"role": "assistant", "content": answer}}]})
            print(f"[backend] request #{number} answered", flush=True)
        except (BrokenPipeError, ConnectionResetError):
            print(f"[backend] request #{number} client disconnected before the answer", flush=True)


print(f"[backend] listening on http://{args.host}:{args.port}/v1 delay={args.delay}s", flush=True)
try:
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
except KeyboardInterrupt:
    print(f"[backend] stopped after {counter} request(s)", flush=True)
