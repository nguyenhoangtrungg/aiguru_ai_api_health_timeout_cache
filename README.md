# AI Guru #54 — Health check, timeout và cache cho AI API

Mở rộng API chat của bài #53 ([aiguru_chat_api_demo](https://github.com/nguyenhoangtrungg/aiguru_chat_api_demo)) bằng ba chức năng:

- `GET /health`: kiểm tra liveness, không gọi model, không đụng cache.
- Deadline cho bước gọi model (`MODEL_DEADLINE_SECONDS`): quá hạn trả HTTP 504 `model_timeout`.
- Cache câu trả lời trong RAM, có TTL và giới hạn số mục (FIFO). Header `X-Cache: MISS/HIT/BYPASS`.

Mỗi request có `X-Request-ID` và một dòng JSON trong `logs/requests.jsonl` để phân biệt request bình thường, timeout và cache hit. Không có câu trả lời giả khi backend lỗi.

## Phiên bản đi kèm bài viết

Mã trong repository này giống hệt ZIP đi kèm bài #54. Có thể clone repository hoặc dùng ZIP; hai cách cho cùng mã và test.

## Chạy trên Linux/bash

Đã thử trên Ubuntu 20.04.6 LTS, Bash 5.0.17, Python 3.12.9. Cần `git`, `curl`, `ss` (iproute2) và Python 3.12 có venv.

Giải nén ZIP rồi mở terminal tại thư mục chứa `main.py` (hoặc từ thư mục cha chạy lệnh `cd` dưới đây):

```bash
cd aiguru_ai_api_health_timeout_cache
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m pip check
python -m pytest -q
cp .env.example .env
```

Sửa `.env`: thay `LLM_MODEL` và `LLM_API_KEY` bằng model ID và key OpenRouter hợp lệ (hoặc base URL/model của server tương thích Chat Completions). Không đưa key thật vào Git. Biến môi trường của shell ghi đè giá trị trong `.env`; biến shell đặt rỗng (`TEN_BIEN=`) dùng mặc định của code, không lấy giá trị trong `.env`. Cấu hình chỉ được đọc lúc khởi động.

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `LLM_TIMEOUT_SECONDS` | 60 | Timeout từng pha HTTPX (connect/read/write/pool) |
| `MODEL_DEADLINE_SECONDS` | 30 | Tổng thời gian tối đa của bước gọi model |
| `CACHE_TTL_SECONDS` | 60 | Thời gian sống của một câu trả lời trong cache |
| `CACHE_MAX_ENTRIES` | 100 | Số câu trả lời tối đa; đầy thì xóa mục thêm sớm nhất |
| `REQUEST_LOG_FILE` | `logs/requests.jsonl` | File log JSONL, đường dẫn tương đối tính từ thư mục project |

Xem cấu hình hiệu lực bằng `python show_config.py`; lệnh đọc cả shell và `.env`, chỉ in danh sách trường cho phép và `api_key_set`, không in key. Không dùng lệnh lọc văn bản để chia sẻ nội dung `.env`.

`message` phải là chuỗi 1–2.000 ký tự sau trim hai đầu; không nhận trường thừa. Sai hợp đồng trả 422 trước khi gọi model. API đổi lỗi kết nối và provider 429/5xx thành 503; status này không chỉ có nghĩa là không kết nối được backend.

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

Gửi một câu hỏi chưa có trong cache và lưu header của response:

```bash
curl -sS -i --max-time 30 -D timeout.headers \
  http://127.0.0.1:18054/chat \
  -H 'Content-Type: application/json' --data '{"message":"Timeout là gì?"}'
python request_logging.py logs/requests.jsonl --headers timeout.headers
```

Nhận HTTP 504 sau khoảng 1 giây. Bản ghi JSON có `"outcome": "model_timeout"` và `"timeout_kind": "model_deadline"`. `GET http://127.0.0.1:18055/stats` cho biết backend đã nhận bao nhiêu request.

File JSONL được ghi nối tiếp qua các lần restart. Dùng `--headers` hoặc `--request-id ID` để lấy đúng bản ghi, tránh chọn timeout cũ. Traceback lỗi ngoài dự kiến ghi qua `uvicorn.error` kèm ID; JSONL có một bản ghi/request. Traceback có thể chứa nội dung exception; chỉ chia sẻ bản ghi đã kiểm tra.

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

Test chẩn đoán không dùng socket/không gọi model thật:

```bash
python -m pytest -q tests/test_api.py::test_unexpected_error_is_logged_as_500
python -m pytest -q tests/test_revision.py
```

Bộ test có 93 ca. Với venv tạo từ `requirements-lock.txt`, `python -m pytest -q` cho `92 passed, 1 skipped` (ca bị bỏ qua là test live). Các test tích hợp mở cổng local nên môi trường chạy test phải cho phép tạo socket.
