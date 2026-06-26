"""
エコリング会員限定・共有ダッシュボード（認証サーバー）

    python3 auth_server.py

閲覧者はエコリング会員のメール/パスワードでログイン → 本人確認OKなら
先生が収集したデータを閲覧できる。非会員はログインが通らず一切見られない。

セキュリティ：
- 閲覧者のメール/パスワードは auth.verify_member で照合するだけ。保存・ログ出力しない。
- 認証成功でランダムなセッショントークンを発行し HttpOnly Cookie で配布（メモリ保持）。
- /api/items はセッションが無いと 401（データは一切返さない）。
- ログイン試行はIPごとにレート制限（総当たり対策）。
公開は Tailscale Funnel（HTTPS・固定URL）で行う想定。
"""
import json
import os
import secrets
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth
import collector
import server  # query_items を流用（先生の収集データを返す）

PORT = 8782
SESSION_TTL = 12 * 3600          # ログインの有効時間（秒）
RL_WINDOW = 300                  # レート制限の窓（秒）
RL_MAX = 8                       # 窓内の最大ログイン試行回数/IP

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

_sessions = {}                   # token -> 失効epoch
_login_hits = {}                 # ip -> [試行epoch,...]
_lock = threading.Lock()


def _now():
    return time.time()


def new_session():
    t = secrets.token_urlsafe(24)
    with _lock:
        _sessions[t] = _now() + SESSION_TTL
    return t


def session_valid(token):
    if not token:
        return False
    with _lock:
        exp = _sessions.get(token)
        if not exp:
            return False
        if exp < _now():
            _sessions.pop(token, None)
            return False
        return True


def drop_session(token):
    with _lock:
        _sessions.pop(token, None)


def rate_ok(ip):
    with _lock:
        arr = [t for t in _login_hits.get(ip, []) if t > _now() - RL_WINDOW]
        if len(arr) >= RL_MAX:
            _login_hits[ip] = arr
            return False
        arr.append(_now())
        _login_hits[ip] = arr
        return True


def get_cookie(headers, name):
    raw = headers.get("Cookie", "")
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", set_cookie=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _client_ip(self):
        # Tailscale Funnel 経由でも実IPに依存しすぎないよう、転送ヘッダ優先
        fwd = self.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _authed(self):
        return session_valid(get_cookie(self.headers, "sid"))

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
                return
            if path == "/api/me":
                self._send(200, {"authed": self._authed()})
                return
            if path == "/api/items":
                if not self._authed():
                    self._send(401, {"error": "unauthorized"})
                    return
                params = urllib.parse.parse_qs(parsed.query)
                self._send(200, server.query_items(params))
                return
            self._send(404, {"error": "not found"})
        except Exception:  # noqa: BLE001
            print("GETエラー:", traceback.format_exc())
            self._send(500, {"error": "internal"})

    def do_POST(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/login":
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    data = {}
                email = (data.get("email") or "").strip()
                password = data.get("password") or ""
                if not rate_ok(self._client_ip()):
                    self._send(429, {"ok": False, "error": "試行が多すぎます。少し待って下さい"})
                    return
                ok = auth.verify_member(email, password)   # creds は保存しない
                if ok:
                    token = new_session()
                    cookie = (f"sid={token}; HttpOnly; SameSite=Lax; Path=/; "
                              f"Max-Age={SESSION_TTL}")
                    self._send(200, {"ok": True}, set_cookie=cookie)
                else:
                    self._send(200, {"ok": False, "error": "エコリングにログインできませんでした（会員情報をご確認ください）"})
                return
            if parsed.path == "/api/logout":
                drop_session(get_cookie(self.headers, "sid"))
                self._send(200, {"ok": True}, set_cookie="sid=; Max-Age=0; Path=/")
                return
            self._send(404, {"error": "not found"})
        except Exception:  # noqa: BLE001
            print("POSTエラー:", traceback.format_exc())
            self._send(500, {"error": "internal"})


def main():
    collector.init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"エコリング会員限定ダッシュボード: http://localhost:{PORT}/")
    print("（公開は Tailscale Funnel で。先生のデータ収集は collector.py を別途定期実行）")
    print("終了は Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")


if __name__ == "__main__":
    main()
