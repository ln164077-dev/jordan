import os
import re
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

from ideco_client import IDECOFetchError, fetch_receivable, start_background_refresh

app = Flask(__name__, static_folder="static", static_url_path="/static")
start_background_refresh()

CACHE_TTL = 600  # 10 دقائق
_cache = {}
_cache_lock = threading.Lock()


def _cache_get(subscriber: str, allow_stale: bool = False):
    with _cache_lock:
        entry = _cache.get(subscriber)
        if entry is None:
            return None
        if allow_stale or time.monotonic() - entry["time"] < CACHE_TTL:
            return entry["data"]
    return None


def _cache_set(subscriber: str, data: dict):
    with _cache_lock:
        _cache[subscriber] = {"time": time.monotonic(), "data": data}
        # منع تراكم الذاكرة
        if len(_cache) > 500:
            oldest = min(_cache, key=lambda k: _cache[k]["time"])
            _cache.pop(oldest, None)


@app.after_request
def compress_response(response):
    accept = request.headers.get("Accept-Encoding", "")
    if (
        "gzip" not in accept
        or response.status_code < 200
        or response.status_code >= 300
        or "Content-Encoding" in response.headers
    ):
        return response
    import gzip

    compressed = None
    data = response.get_data(as_text=False) if not response.direct_passthrough else None
    if data is None:
        return response
    compressed = gzip.compress(data)
    if len(compressed) >= len(data):
        return response
    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = len(compressed)
    response.headers["Vary"] = "Accept-Encoding"
    return response


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/form")
def form():
    return send_from_directory(app.static_folder, "form.html")


@app.post("/api/lookup")
def lookup():
    data = request.get_json(silent=True) or {}
    subscriber = (data.get("subscriber") or "").strip()
    mode = (data.get("mode") or "live").strip()

    if not re.fullmatch(r"\d{10}", subscriber):
        return jsonify({"error": "رقم الاشتراك يجب أن يتكون من 10 خانات رقمية"}), 400

    cached = _cache_get(subscriber)
    if cached is not None:
        return jsonify({**cached, "cached": True})

    if mode == "snapshot":
        stale = _cache_get(subscriber, allow_stale=True)
        if stale is not None:
            return jsonify({**stale, "stale": True})
        return ("", 204)

    try:
        result = fetch_receivable(subscriber)
        _cache_set(subscriber, result)
        return jsonify(result)
    except IDECOFetchError:
        return jsonify({"error": "تعذر الاتصال بموقع شركة الكهرباء، يرجى المحاولة لاحقاً"}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 12000)), threaded=True)
