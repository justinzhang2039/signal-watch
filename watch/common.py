"""公共工具：路径、时间、读写、HTTP。只用标准库。"""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("WATCH_ROOT") or Path(__file__).resolve().parent.parent)
DATA = ROOT / "data"
DOCS = ROOT / "docs"
UA = "signal-watch/1.0 (github actions; public data only)"
MAX_BYTES = 8 * 1024 * 1024


def now():
    """当前 UTC 时间。测试时可用 WATCH_NOW=2027-01-05T00:00:00 覆盖。"""
    fake = os.environ.get("WATCH_NOW")
    if fake:
        return dt.datetime.fromisoformat(fake).replace(tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def week_key(t=None):
    return (t or now()).strftime("%G-W%V")


def month_key(t=None):
    return (t or now()).strftime("%Y-%m")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path, rows):
    if not rows:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path):
    try:
        with Path(path).open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


class HttpError(RuntimeError):
    """只带状态码，不带响应体和请求头，避免把密钥或内容写进公开日志。"""

    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


def http(url, *, body=None, headers=None, timeout=30, retries=2):
    """GET（body 为空）或 POST JSON。429/5xx 退避重试。返回 bytes。"""
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    hdrs.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(MAX_BYTES)
        except urllib.error.HTTPError as e:
            last = HttpError(e.code)
            if e.code not in (429, 500, 502, 503, 504, 529):
                break
        except Exception as e:  # 超时、DNS 等
            last = RuntimeError(type(e).__name__)
        if attempt < retries:
            time.sleep(float(os.environ.get("WATCH_BACKOFF", "4")) * (attempt + 1))
    raise last
