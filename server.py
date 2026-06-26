"""
エコリング ダッシュボード用サーバー（標準ライブラリのみ）

    python3 server.py

http://localhost:8781/ を開くと、収集した商品一覧が見られます。
各自のPC内で動かす想定（ローカル専用）です。
"""
import json
import os
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import collector

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
PORT = 8781
JST = timezone(timedelta(hours=9))
AUTO_REFRESH_MINUTES = 60
PAGES = 1

_lock = threading.Lock()
_state = {"running": False, "log": [], "last": None}


def run_collection(pages=PAGES):
    if not _lock.acquire(blocking=False):
        return False
    _state["running"] = True
    _state["log"] = []
    try:
        collector.collect(pages=pages, log=lambda m: _state["log"].append(m))
        _state["last"] = datetime.now(JST).isoformat()
    except Exception as e:  # noqa: BLE001
        _state["log"].append(f"収集中にエラー: {e}")
    finally:
        _state["running"] = False
        _lock.release()
    return True


def auto_refresh_loop():
    while True:
        time.sleep(max(1, AUTO_REFRESH_MINUTES) * 60)
        threading.Thread(target=run_collection, daemon=True).start()


def query_items(params):
    collector.init_db()
    conn = collector.get_db()
    try:
        return _query_items(conn, params)
    finally:
        conn.close()


def _query_items(conn, params):
    where, args = [], []

    q = params.get("q", [""])[0].strip()
    if q:
        where.append("title LIKE ?"); args.append(f"%{q}%")
    keyword = params.get("keyword", [""])[0].strip()
    if keyword:
        where.append("keyword = ?"); args.append(keyword)
    pmin = params.get("min", [""])[0].strip()
    if pmin.isdigit():
        where.append("current_price >= ?"); args.append(int(pmin))
    pmax = params.get("max", [""])[0].strip()
    if pmax.isdigit():
        where.append("current_price <= ?"); args.append(int(pmax))

    sort = params.get("sort", ["new"])[0]
    order = {
        "new": "first_seen DESC, rowid DESC",
        "price_asc": "current_price IS NULL, current_price ASC",
        "price_desc": "current_price DESC",
    }.get(sort, "first_seen DESC, rowid DESC")

    sql = "SELECT * FROM items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT 600"
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]

    kw_rows = conn.execute(
        "SELECT keyword, COUNT(*) c FROM items GROUP BY keyword ORDER BY keyword"
    ).fetchall()

    def meta(k):
        row = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return row[0] if row else None

    resp = {
        "items": rows,
        "keywords": [r[0] for r in kw_rows],
        "keyword_counts": {r[0]: r[1] for r in kw_rows},
        "total": conn.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "shown": len(rows),
        "last_run": meta("last_run"),
        "warning": meta("parse_warning") or "",
    }
    return resp


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ct="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/items":
                self._send(200, query_items(urllib.parse.parse_qs(parsed.query)))
                return
            if parsed.path == "/api/status":
                self._send(200, {"running": _state["running"], "log": _state["log"], "last": _state["last"]})
                return
            self._send(404, {"error": "not found"})
        except Exception:  # noqa: BLE001
            self._send(500, {"error": "internal", "detail": traceback.format_exc(limit=2)})

    def do_POST(self):
        try:
            if urllib.parse.urlparse(self.path).path == "/api/refresh":
                if _state["running"]:
                    self._send(200, {"started": False, "reason": "already_running"})
                    return
                threading.Thread(target=run_collection, daemon=True).start()
                self._send(200, {"started": True})
                return
            self._send(404, {"error": "not found"})
        except Exception:  # noqa: BLE001
            self._send(500, {"error": "internal"})


def main():
    collector.init_db()
    conn = collector.get_db()
    count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    conn.close()
    if count == 0:
        threading.Thread(target=run_collection, daemon=True).start()
    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"エコリング ダッシュボード: http://localhost:{PORT}/")
    print("終了するには Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")


if __name__ == "__main__":
    main()
