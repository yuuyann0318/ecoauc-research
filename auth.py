"""
閲覧者がエコリング会員かを確認する（本人確認のみ・認証情報は保存しない）。

共有ダッシュボードの「ログインしさえすれば見れる」を実現する核。
閲覧者のメール/パスワードで *その場で* エコリングにログインを試み、成功＝会員と判定する。
- 先生の収集用セッション(collector のグローバル Cookie)は汚さない（専用の使い捨て Cookie を使う）
- メール/パスワードはメモリ上だけ。ディスク保存もログ出力も一切しない。
"""
import gzip
import http.cookiejar
import urllib.parse
import urllib.request

import collector  # URL定数・USER_AGENT・CSRF抽出・ログイン判定を流用


def verify_member(email, password, timeout=25):
    """エコリングにログインできれば True（＝会員）。失敗・例外は False。"""
    if not email or not password:
        return False

    jar = http.cookiejar.CookieJar()                       # 使い捨て・保存しない
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def req(url, data=None, headers=None):
        h = {
            "User-Agent": collector.USER_AGENT,
            "Accept-Language": "ja,en;q=0.9",
            "Accept-Encoding": "gzip",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if headers:
            h.update(headers)
        r = opener.open(urllib.request.Request(url, data=data, headers=h), timeout=timeout)
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return r, raw.decode("utf-8", "ignore")

    try:
        r, page = req(collector.LOGIN_URL)
        token = collector._extract_csrf(page)
        if not token:
            return False
        form = urllib.parse.urlencode({
            "_method": "POST",
            "_csrfToken": token,
            "email_address": email,
            "password": password,
            "remember-me": "remember-me",
        }).encode("utf-8")
        r, page = req(
            collector.POST_LOGIN_URL,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Referer": collector.LOGIN_URL, "Origin": collector.BASE_URL},
        )
        return bool(collector.is_logged_in_html(page, r.geturl()))
    except Exception:  # noqa: BLE001
        return False
    finally:
        jar.clear()   # 念のため明示的に破棄
