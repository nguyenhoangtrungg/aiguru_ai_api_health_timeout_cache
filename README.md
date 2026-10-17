# AI Guru #54 — Health check, timeout và cache cho AI API

Mở rộng API chat của bài #53 ([aiguru_chat_api_demo](https://github.com/nguyenhoangtrungg/aiguru_chat_api_demo)) bằng ba chức năng:

- `GET /health`: kiểm tra liveness, không gọi model, không đụng cache.
- Deadline cho bước gọi model (`MODEL_DEADLINE_SECONDS`): quá hạn trả HTTP 504 `model_timeout`.
- Cache câu trả lời trong RAM, có TTL và giới hạn số mục (FIFO). Header `X-Cache: MISS/HIT/BYPASS`.

Mỗi request có `X-Request-ID` và một dòng JSON trong `logs/requests.jsonl` để phân biệt request bình thường, timeout và cache hit. Không có câu trả lời giả khi backend lỗi.

## Chạy trên Linux/bash

Đã thử trên Ubuntu 20.04.6 LTS, Bash 5.0.17, Python 3.12.9. Cần `git`, `curl`, `ss` (iproute2) và Python 3.12 có venv.

```bash
git clone https://github.com/nguyenhoangtrungg/aiguru_ai_api_health_timeout_cache.git
cd aiguru_ai_api_health_timeout_cache
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m pip check
python -m pytest -q
cp .env.example .env
```

Sửa `.env`: thay `LLM_MODEL` và `LLM_API_KEY` bằng model ID và key OpenRouter hợp lệ (hoặc base URL/model của server tương thích Chat Completions). Không đưa key thật vào Git. Biến môi trường của shell ghi đè giá trị trong `.env`; cấu hình chỉ được đọc lúc khởi động.

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `LLM_TIMEOUT_SECONDS` | 60 | Timeout từng pha HTTPX (connect/read/write/pool) |
| `MODEL_DEADLINE_SECONDS` | 30 | Tổng thời gian tối đa của bước gọi model |
| `CACHE_TTL_SECONDS` | 60 | Thời gian sống của một câu trả lời trong cache |
| `CACHE_MAX_ENTRIES` | 100 | Số câu trả lời tối đa; đầy thì xóa mục thêm sớm nhất |
| `REQUEST_LOG_FILE` | `logs/requests.jsonl` | File log JSONL, đường dẫn tương đối tính từ thư mục project |

Terminal A, không dùng `--reload` (reload khởi động lại tiến trình và xóa cache):

```bash
ss -ltn '( sport = :18054 )'
python -m uvicorn main:app --host 127.0.0.1 --port 18054
```

Terminal B:

```bash
curl -sS -i http://127.0.0.1:18054/health
curl -sS -i http://127.0.0.1:18054/chat -H 'Content-Type: application/json' --data '{"message":"Cache là gì?"}'
curl -sS -i http://127.0.0.1:18054/chat -H 'Content-Type: application/json' --data '{"message":"Cache là gì?"}'
python request_logging.py logs/requests.jsonl
```

Lần chat đầu có `X-Cache: MISS`, lần hai `X-Cache: HIT` và cùng `answer`. Swagger: <http://127.0.0.1:18054/docs>.

## Demo timeout bằng backend thử nghiệm

`demo/slow_backend.py` là server HTTP giả lập Chat Completions có độ trễ, **không có model thật**. Terminal C:

```bash
python demo/slow_backend.py --port 18055 --delay 3
```

Dừng API ở terminal A rồi chạy lại với bộ cấu hình demo. Các biến đặt trước lệnh chỉ áp dụng cho lệnh đó, không sửa `.env`:

```bash
LLM_BASE_URL=http://127.0.0.1:18055/v1 LLM_MODEL=demo-backend LLM_API_KEY= \
LLM_TIMEOUT_SECONDS=10 MODEL_DEADLINE_SECONDS=1 CACHE_TTL_SECONDS=5 \
python -m uvicorn main:app --host 127.0.0.1 --port 18054
```

Gửi một câu hỏi chưa có trong cache: nhận HTTP 504 sau khoảng 1 giây. Log ghi `outcome=model_timeout`, `timeout_kind=model_deadline`. `GET http://127.0.0.1:18055/stats` cho biết backend đã nhận bao nhiêu request.

## Kiểm thử

```bash
python -m pytest -q
```

Gồm test hợp đồng (MockTransport), cache/TTL/FIFO bằng đồng hồ giả, deadline, request đồng thời, cấu hình, log và hai test tích hợp khởi chạy Uvicorn + backend thử nghiệm thật qua HTTP. `tests/test_live.py` gọi model thật, chỉ chạy khi đặt `RUN_LIVE_TESTS=1` và `.env` hợp lệ; nếu không sẽ được báo `skipped`.

## Giới hạn

- Cache nằm trong RAM của một tiến trình: restart xóa cache; nhiều worker có cache riêng. Hai request trùng đến cùng lúc có thể cùng gọi backend.
- Cache chung cho mọi người gọi; chỉ phù hợp câu hỏi công khai, một lượt. Không dùng cho dữ liệu cá nhân.
- Câu hỏi giống nhau trong TTL nhận lại câu trả lời đã lưu, kể cả khi model có thể trả khác.
- Deadline dừng việc chờ ở API; không bảo đảm provider ngừng xử lý hoặc không tính phí.
- `/health` chỉ là liveness, không chứng minh model sẵn sàng.
- API local, chưa có xác thực client, streaming hay triển khai public.
