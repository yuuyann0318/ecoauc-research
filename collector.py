"""
エコリング(EcoRing the Auction / ecoauc.com) 収集エンジン（標準ライブラリのみ）

会員ログインが必要なため、HTTPフォームログイン（CSRF＋Cookie）で認証し、
キーワード検索の結果を SQLite (db.sqlite) に保存します。Playwrightは不要。

認証情報は config.txt（このフォルダ内）に置きます。外部には一切送信しません。

使い方:
    python3 collector.py --setup     # 初回ログイン＆動作確認（サンプルHTMLも保存）
    python3 collector.py             # 収集（セッションは再利用）
"""
import argparse
import gzip
import html
import http.cookiejar
import os
import re
import sqlite3
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "db.sqlite")
KEYWORDS_PATH = os.path.join(BASE_DIR, "keywords.txt")
CONFIG_PATH = os.path.join(BASE_DIR, "config.txt")
COOKIE_PATH = os.path.join(BASE_DIR, "session_cookies.txt")
# 落札相場用サンプル（互換のため残す）と即決用サンプルを分ける
SAMPLE_PATH = os.path.join(BASE_DIR, "eco_sample.html")
BUYNOW_SAMPLE_PATH = os.path.join(BASE_DIR, "eco_buynow_sample.html")

BASE_URL = "https://www.ecoauc.com"
LOGIN_URL = f"{BASE_URL}/client/users/sign_in"
POST_LOGIN_URL = f"{BASE_URL}/client/users/post-sign-in"

# データ源の切替フラグ。
#   "buynow" … 入札受付中の即決商品（/client/buy-now）を一次ソースにする（既定・2026/6/29）。
#   "market" … 落札相場（過去・/client/market-prices）をパースする（フォールバック）。
# どちらもサーバー描画HTML＋グリッド構造で、正規表現パースと収集ループは共通。
SOURCE = "buynow"

MARKET_URL = f"{BASE_URL}/client/market-prices"
BUYNOW_URL = f"{BASE_URL}/client/buy-now"
SEARCH_URL = BUYNOW_URL if SOURCE == "buynow" else MARKET_URL
# 実際に dump で書き出すサンプルHTMLのパス（ソースで切替）
ACTIVE_SAMPLE_PATH = BUYNOW_SAMPLE_PATH if SOURCE == "buynow" else SAMPLE_PATH

JST = timezone(timedelta(hours=9))
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
POLITE_DELAY = 2.5
_VALID_ID = re.compile(r"^\d+$")

# 落札相場の検索はスペース=OR（語のどれかを含む全件）かつ他ブランド混在のため、
# 収集側で「ブランド一致＋商品名一致」のAND絞り込みを行う（2026/6/17）。
PAGES_PER_KEYWORD = 4  # OR検索で関連品が散るため複数ページ集める

# キーワード先頭のブランド語 → 相場タイトル/ブランド欄での表記ゆれ
BRAND_ALIAS = {
    "ティファニー": ["tiffany", "ティファニー"],
    "ルイヴィトン": ["louis vuitton", "louisvuitton", "ルイヴィトン", "ルイ・ヴィトン", "lv"],
    "ルイ・ヴィトン": ["louis vuitton", "louisvuitton", "ルイヴィトン", "ルイ・ヴィトン", "lv"],
    "シャネル": ["chanel", "シャネル"],
    "エルメス": ["hermes", "hermès", "エルメス"],
    "グッチ": ["gucci", "グッチ"],
    "カルティエ": ["cartier", "カルティエ"],
}
# それ単独では商品を特定しない一般カテゴリ語（AND必須にしない）
GENERIC_WORDS = {
    "リング", "指輪", "ネックレス", "ペンダント", "ブレスレット", "バングル",
    "ピアス", "イヤリング", "チェーン", "財布", "バッグ", "時計", "ウォッチ",
}
_GENERIC_SUFFIX = ("リング", "ネックレス", "ペンダント", "ブレスレット",
                   "バングル", "ピアス", "チェーン", "バッグ")


def _norm(s):
    """全半角・大小文字・カタカナのヴ表記ゆれ(ラヴィング↔ラビング等)を吸収。"""
    s = unicodedata.normalize("NFKC", s or "").lower()
    for a, b in (("ヴぃ", "び"), ("ヴぇ", "べ"), ("ヴぁ", "ば"), ("ヴぉ", "ぼ"),
                 ("ヴ", "ぶ"), ("ヴィ", "ビ"), ("ヴェ", "ベ"), ("ヴァ", "バ"),
                 ("ヴォ", "ボ"), ("ヴ", "ブ")):
        s = s.replace(a, b)
    return s


def match_keyword(item, keyword):
    """item がキーワードの「ブランド＋商品名」条件を満たすか（AND）。"""
    title = _norm(item.get("title", ""))
    brand = _norm(item.get("brand", ""))
    for tok in keyword.replace("　", " ").split():
        if tok in BRAND_ALIAS:
            if not any(_norm(a) in title or _norm(a) in brand
                       for a in BRAND_ALIAS[tok]):
                return False
        elif tok in GENERIC_WORDS:
            continue  # 一般語は必須にしない（タイトル省略が多いため）
        else:
            cands = [tok]
            for suf in _GENERIC_SUFFIX:  # 「アトラスリング」→「アトラス」も許容
                if tok.endswith(suf) and len(tok) > len(suf):
                    cands.append(tok[: -len(suf)])
            if not any(_norm(c) in title for c in cands):
                return False
    return True


def now_jst():
    return datetime.now(JST)


# ----------------------------------------------------------------------
# HTTP（Cookie保持）
# ----------------------------------------------------------------------
_cookiejar = http.cookiejar.MozillaCookieJar(COOKIE_PATH)
if os.path.exists(COOKIE_PATH):
    try:
        _cookiejar.load(ignore_discard=True, ignore_expires=True)
    except Exception:  # noqa: BLE001
        pass
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookiejar))


def _request(url, data=None, headers=None, timeout=30):
    h = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en;q=0.9",
        "Accept-Encoding": "gzip",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    r = _opener.open(req, timeout=timeout)
    raw = r.read()
    if r.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return r, raw.decode("utf-8", "ignore")


def _save_cookies():
    try:
        _cookiejar.save(ignore_discard=True, ignore_expires=True)
        os.chmod(COOKIE_PATH, 0o600)   # 他ユーザーから読めないように
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------
# 認証
# ----------------------------------------------------------------------
def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            os.chmod(CONFIG_PATH, 0o600)   # 認証情報ファイルを他ユーザーから守る
        except OSError:
            pass
        with open(CONFIG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def _extract_csrf(page_html):
    m = re.search(r'name="_csrfToken"[^>]*value="([^"]+)"', page_html)
    if not m:
        m = re.search(r'value="([^"]+)"[^>]*name="_csrfToken"', page_html)
    return m.group(1) if m else None


def is_logged_in_html(page_html, final_url):
    """ログイン状態かを判定。
    ・サインインページに居る/ログインフォームがある＝未ログイン
    ・ログアウトリンク等の「ログイン後だけ出る印」があれば確実にログイン済み
    どちらの印も無い（エラー/メンテ等）ときは安全側に倒して False（未ログイン扱い）。
    """
    if "sign_in" in final_url:
        return False
    if 'name="email_address"' in page_html and 'name="password"' in page_html:
        return False
    # ログイン後だけ出る会員ナビ/印（実ログイン後の /client DOM で確認済み 2026/6/17）
    markers = (
        "sign_out", "sign-out", "ログアウト", "/client/items/",
        "/client/mylist", "/client/bids", "/client/auctions",
        "/client/market-prices", "/client/buy-now",
        "/client/auction-items/", "マイリスト",
    )
    if any(m in page_html for m in markers):
        return True
    return False


def login(log=print):
    cfg = load_config()
    email = cfg.get("email", "")
    password = cfg.get("password", "")
    if not email or not password:
        log("✗ config.txt に email と password を設定してください。")
        return False

    # 1) ログインページを取得して CSRF トークンと初期Cookieを得る
    try:
        r, page = _request(LOGIN_URL)
    except Exception as e:  # noqa: BLE001
        log(f"✗ ログインページ取得に失敗: {e}")
        return False
    token = _extract_csrf(page)
    if not token:
        log("✗ CSRFトークンが見つかりません（サイト構造が変わった可能性）。")
        return False

    # 2) 認証情報をPOST
    form = urllib.parse.urlencode({
        "_method": "POST",
        "_csrfToken": token,
        "email_address": email,
        "password": password,
        "remember-me": "remember-me",
    }).encode("utf-8")
    try:
        r, page = _request(
            POST_LOGIN_URL,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Referer": LOGIN_URL, "Origin": BASE_URL},
        )
    except Exception as e:  # noqa: BLE001
        log(f"✗ ログインPOSTに失敗: {e}")
        return False

    if is_logged_in_html(page, r.geturl()):
        _save_cookies()
        log("✓ ログイン成功")   # メールアドレスはログに出さない（/api/statusに載るため）
        return True

    # 失敗時はエラーメッセージを拾う
    m = re.search(r'(alert[^>]*>|class="[^"]*error[^"]*"[^>]*>)(.*?)<', page, re.S)
    reason = re.sub(r"<[^>]+>", "", m.group(2)).strip()[:80] if m else "メール/パスワードをご確認ください"
    log(f"✗ ログイン失敗: {reason}")
    return False


def ensure_logged_in(log=print):
    # 既存セッションが生きているかを軽くチェック
    try:
        r, page = _request(f"{SEARCH_URL}", timeout=20)
        if is_logged_in_html(page, r.geturl()):
            return True
    except Exception:  # noqa: BLE001
        pass
    return login(log=log)


# ----------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id            TEXT PRIMARY KEY,
            title         TEXT,
            url           TEXT,
            image         TEXT,
            current_price INTEGER,
            keyword       TEXT,
            brand         TEXT,
            rank          TEXT,
            category      TEXT,
            shape         TEXT,
            sold_date     TEXT,
            auction       TEXT,
            start_price   INTEGER,
            buy_now_price INTEGER,
            unit_price    INTEGER,
            start_at      TEXT,
            auction_round TEXT,
            lane          TEXT,
            lot_position  TEXT,
            my_prebid     INTEGER,
            first_seen    TEXT,
            last_seen     TEXT
        )
        """
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    # 旧スキーマからのマイグレーション（不足列を型付きで追加）。
    # 既存の落札相場用カラムに加え、即決(buy-now)用カラムを足す（2026/6/29）。
    migrations = {
        "brand": "TEXT", "rank": "TEXT", "category": "TEXT", "shape": "TEXT",
        "sold_date": "TEXT", "auction": "TEXT",
        "start_price": "INTEGER", "buy_now_price": "INTEGER",
        "unit_price": "INTEGER", "start_at": "TEXT", "auction_round": "TEXT",
        "lane": "TEXT", "lot_position": "TEXT", "my_prebid": "INTEGER",
    }
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for col, typ in migrations.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
    conn.commit()
    conn.close()


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# ----------------------------------------------------------------------
# 検索・解析
# ----------------------------------------------------------------------
def fetch_search(keyword, page=1):
    """一覧ページを取得する。SOURCE により URL とクエリ条件を切り替える。"""
    if SOURCE == "buynow":
        # 即決一覧（グリッド表示・新着順）。空文字パラメータも本家の挙動に合わせて付与。
        params = {
            "q": keyword, "limit": 50, "tableType": "grid", "sortKey": 1,
            "master_item_ranks": "", "auction_lane_id": "",
        }
    else:
        params = {"q": keyword, "limit": 50}
    if page > 1:
        params["page"] = page
    url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
    r, page_html = _request(url)
    return r.geturl(), page_html


def _card_field(card, pattern, group=1, unescape=True):
    m = re.search(pattern, card, re.S)
    if not m:
        return ""
    val = m.group(group)
    if unescape:
        val = html.unescape(val)
    return re.sub(r"\s+", " ", val).strip()


def parse_search(page_html, keyword, dump=False):
    """SOURCE に応じて即決(parse_buynow)/落札相場(parse_market_prices)へ振り分ける。

    既定は即決(buy-now)。SOURCE="market" にすると落札相場パーサに切り替わる。
    収集ループ(collect)からはこの関数だけを呼べばよい。
    """
    if SOURCE == "buynow":
        return parse_buynow(page_html, keyword, dump=dump)
    return parse_market_prices(page_html, keyword, dump=dump)


def parse_market_prices(page_html, keyword, dump=False):
    """エコリングの落札相場（/client/market-prices）から商品カードを抽出する。

    実ログイン後のDOMを確認して構造ベースで解析（2026/6/17）。1カードは
    `<div class="col-sm-6 col-md-4 col-lg-3"> ... <a href=".../view/ID"> ... </a></div>`。
    取得項目: ブランド/落札日/タイトル/ランク/カテゴリ/形状/落札価格/画像/開催回。
    ※即決ソースへ切替後はフォールバックとして温存（2026/6/29）。
    """
    if dump:
        try:
            with open(SAMPLE_PATH, "w", encoding="utf-8") as f:
                f.write(page_html)
        except Exception:  # noqa: BLE001
            pass

    # グリッドの各カードに分割
    starts = [m.start() for m in
              re.finditer(r'<div class="col-sm-6 col-md-4 col-lg-3">', page_html)]
    items = []
    seen = set()
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(page_html)
        card = page_html[s:e]

        idm = re.search(r'/client/market-prices/view/(\d+)', card)
        if not idm:
            continue
        aid = idm.group(1)
        if aid in seen or not _VALID_ID.match(aid):
            continue
        seen.add(aid)
        url = BASE_URL + "/client/market-prices/view/" + aid

        brand = _card_field(card, r'class="show-case-bland">([^<]+)<')
        sold_date = _card_field(card, r'class="show-case-daily">([^<]+)<')
        title = _card_field(card, r'<b>(.*?)</b>')
        rank = _card_field(card, r'class="canopy-rank">([^<]+)<')
        auction = _card_field(card, r'class="market-title">(.*?)</span>')
        auction = re.sub(r"<[^>]+>", " ", auction)
        auction = re.sub(r"\s+", " ", auction).strip()
        category = _card_field(
            card, r'カテゴリ</small>\s*<span class="canopy-value">([^<]+)<')
        shape = _card_field(
            card, r'形状コード</small>\s*<span class="canopy-value">([^<]+)<')

        # 画像（resize.ecoauc.com のサムネ。クエリは外して原寸寄せ）
        img = ""
        im = re.search(r'<img[^>]+src="(https://resize\.ecoauc\.com/[^"]+)"', card)
        if not im:
            im = re.search(r'<img[^>]+src="([^"]+)"', card)
        if im:
            img = im.group(1)
            if img.startswith("//"):
                img = "https:" + img
            elif img.startswith("/"):
                img = BASE_URL + img

        # 落札価格
        price = None
        pm = re.search(
            r'落札価格</span>\s*<span[^>]*show-value[^>]*>\s*(?:&yen;|¥|￥)?\s*([0-9,]+)',
            card)
        if not pm:
            pm = re.search(r'(?:&yen;|¥|￥)\s*([0-9][0-9,]{1,})', card)
        if pm:
            try:
                price = int(pm.group(1).replace(",", ""))
            except ValueError:
                price = None

        items.append({
            "id": aid, "title": title, "url": url, "image": img,
            "current_price": price, "keyword": keyword,
            "brand": brand, "rank": rank, "category": category,
            "shape": shape, "sold_date": sold_date, "auction": auction,
        })
    return items


def _to_int(s):
    """カンマ区切りの金額文字列を int に。数値が無ければ None。"""
    if s is None:
        return None
    try:
        return int(str(s).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def parse_buynow(page_html, keyword, dump=False):
    """エコリングの即決一覧（/client/buy-now・入札受付中）から商品カードを抽出する。

    実HTML(recon/buy_now.html)で構造を確認済み（2026/6/29）。1カードは
    `<div class="col-sm-6 col-md-4 col-lg-3 mb-grid-card"> ... </div>`。
    取得項目: ブランド/タイトル/画像/即決価格/開始価格/入札単位/ランク/出品順位/
    開催回/開始日時/レーン名/事前入札額(あなたの入札額)。
    売却済み(SOLD)のカードは収集対象外としてスキップする。
    各フィールドは取得失敗時に None または "" を入れ、1枚の失敗で全体を落とさない。
    """
    if dump:
        try:
            with open(BUYNOW_SAMPLE_PATH, "w", encoding="utf-8") as f:
                f.write(page_html)
        except Exception:  # noqa: BLE001
            pass

    # グリッドの各カードに分割。即決カードは `... mb-grid-card"` クラスを持つので、
    # まず実クラスに合わせた厳密なアンカーで切る（最後のカードがページ末尾のモーダル塊を
    # 飲み込むのを防ぐ）。0件のときだけ、従来のゆるいアンカーへフォールバックする
    # （将来サイトのclassが変わってもグリッドを拾えるように）。
    starts = [m.start() for m in re.finditer(
        r'<div class="col-sm-6 col-md-4 col-lg-3 mb-grid-card', page_html)]
    if not starts:
        starts = [m.start() for m in re.finditer(
            r'<div class="col-sm-6 col-md-4 col-lg-3', page_html)]
    items = []
    seen = set()
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(page_html)
        card = page_html[s:e]

        # ID（/client/auction-items/view/ID/BuyNow?...）
        idm = re.search(r'/client/auction-items/view/(\d+)', card)
        if not idm:
            continue
        aid = idm.group(1)
        if aid in seen or not _VALID_ID.match(aid):
            continue
        seen.add(aid)

        # 売却済み（SOLDバッジに hidden が無ければ売れている）→ 収集対象外
        try:
            sm = re.search(r'class="buy-now-sold-badge([^"]*)"', card)
            if sm and "hidden" not in sm.group(1):
                continue
        except Exception:  # noqa: BLE001
            pass

        # 即決カードの実 href（/client/auction-items/view/{id}/BuyNow?... ）を採用し、
        # BuyNow ルート/クエリ（sortKey/limit 等）を保持する。html.unescape で &amp; を復元。
        # 取れなければ従来どおり ID から URL を再構築してフォールバックする。
        try:
            hm = re.search(r'href="(/client/auction-items/view/\d+[^"]*)"', card)
            url = (BASE_URL + html.unescape(hm.group(1))) if hm \
                else (BASE_URL + "/client/auction-items/view/" + aid)
        except Exception:  # noqa: BLE001
            url = BASE_URL + "/client/auction-items/view/" + aid

        # ブランド
        try:
            brand = _card_field(card, r'class="show-case-bland">([^<]+)<')
        except Exception:  # noqa: BLE001
            brand = ""

        # タイトル（先頭の (12678_0252) のような管理番号は残してよい）
        try:
            title = _card_field(card, r'<b>(.*?)</b>')
        except Exception:  # noqa: BLE001
            title = ""

        # 画像（resize.ecoauc.com のサムネ優先）
        img = ""
        try:
            im = re.search(r'<img[^>]+src="(https://resize\.ecoauc\.com/[^"]+)"', card)
            if not im:
                im = re.search(r'<img[^>]+src="([^"]+)"', card)
            if im:
                img = im.group(1)
                if img.startswith("//"):
                    img = "https:" + img
                elif img.startswith("/"):
                    img = BASE_URL + img
        except Exception:  # noqa: BLE001
            img = ""

        # 即決価格（data属性優先・表示文字列を代替）
        buy_now_price = None
        try:
            m = re.search(r'data-asking-bid-closing-price="(\d+)"', card)
            if m:
                buy_now_price = _to_int(m.group(1))
            else:
                m = re.search(r'class="buy-now-price">[^\d]*([0-9,]+)', card)
                buy_now_price = _to_int(m.group(1)) if m else None
        except Exception:  # noqa: BLE001
            buy_now_price = None

        # 開始価格（data属性優先・カノピー表示を代替）
        start_price = None
        try:
            m = re.search(r'data-starting-price="(\d+)"', card)
            if m:
                start_price = _to_int(m.group(1))
            else:
                m = re.search(
                    r'開始価格</small>\s*<big class="canopy-value">\s*'
                    r'(?:&yen;|¥)?\s*([0-9,]+)', card, re.S)
                start_price = _to_int(m.group(1)) if m else None
        except Exception:  # noqa: BLE001
            start_price = None

        # 入札単位
        unit_price = None
        try:
            m = re.search(r'data-unit-price="(\d+)"', card)
            unit_price = _to_int(m.group(1)) if m else None
        except Exception:  # noqa: BLE001
            unit_price = None

        # ランク（値は </big> の外側にある。取れなければ空文字）
        rank = ""
        try:
            m = re.search(
                r'ランク</small>\s*<big class="canopy-value">\s*</big>\s*([A-Za-z0-9]+)',
                card, re.S)
            rank = m.group(1) if m else ""
        except Exception:  # noqa: BLE001
            rank = ""

        # 出品順位 → "252/420" 形式
        lot_position = ""
        try:
            m = re.search(
                r'出品順位</small>\s*<big class="canopy-value">\s*(\d+)\s*'
                r'&frasl;\s*(\d+)', card, re.S)
            lot_position = f"{m.group(1)}/{m.group(2)}" if m else ""
        except Exception:  # noqa: BLE001
            lot_position = ""

        # market-title（開催回・開始日時・レーン名を含む）
        mt = ""
        try:
            mm = re.search(r'class="market-title">(.*?)</span>', card, re.S)
            mt = mm.group(1) if mm else ""
        except Exception:  # noqa: BLE001
            mt = ""

        # 開催回（第NNN回）
        auction_round = ""
        try:
            m = re.search(r'第(\d+)回', mt)
            auction_round = m.group(1) if m else ""
        except Exception:  # noqa: BLE001
            auction_round = ""

        # 開始日時（（2026-06-26 10:00開始）。全角カッコ優先・半角も許容）
        start_at = ""
        try:
            m = re.search(r'[（(](\d{4}-\d{2}-\d{2} \d{2}:\d{2})開始[）)]', mt)
            start_at = m.group(1) if m else ""
        except Exception:  # noqa: BLE001
            start_at = ""

        # レーン名（<br> 後ろのレーン行を優先し、日時カッコを除去。最悪フルテキスト）
        lane = ""
        try:
            seg = re.split(r'<br\s*/?>', mt)[-1] if mt else mt
            lane = re.sub(r"<[^>]+>", " ", seg)
            lane = re.sub(r"\s+", " ", html.unescape(lane)).strip()
            lane = re.sub(r'[（(]\d{4}-\d{2}-\d{2}.*$', '', lane).strip()
            if not lane:  # 抽出できなければ market-title 全文を整形して入れる
                full = re.sub(r"<[^>]+>", " ", mt)
                lane = re.sub(r"\s+", " ", html.unescape(full)).strip()
        except Exception:  # noqa: BLE001
            lane = ""

        # market-title 全文（落札相場の auction 列と同じ用途で互換保存）
        try:
            auction_full = re.sub(r"<[^>]+>", " ", mt)
            auction_full = re.sub(r"\s+", " ", html.unescape(auction_full)).strip()
        except Exception:  # noqa: BLE001
            auction_full = ""

        # あなたの入札額/事前入札額（"¥ 0" など。取れなければ None）
        my_prebid = None
        try:
            m = re.search(r'item-show-value">\s*¥?\s*([0-9,]+)', card)
            my_prebid = _to_int(m.group(1)) if m else None
        except Exception:  # noqa: BLE001
            my_prebid = None

        # 互換: current_price には start_price（無ければ buy_now_price）を入れる。
        # 既存のソート/フィルタ(price_asc/desc・min/max)がそのまま効くようにするため。
        current_price = start_price if start_price is not None else buy_now_price

        items.append({
            "id": aid, "title": title, "url": url, "image": img,
            "current_price": current_price, "keyword": keyword,
            "brand": brand, "rank": rank,
            # 即決には無い落札相場用フィールドは空で埋める（DB/UI互換のため）
            "category": "", "shape": "", "sold_date": "", "auction": auction_full,
            # 即決固有フィールド
            "start_price": start_price, "buy_now_price": buy_now_price,
            "unit_price": unit_price, "start_at": start_at,
            "auction_round": auction_round, "lane": lane,
            "lot_position": lot_position, "my_prebid": my_prebid,
        })
    return items


# ----------------------------------------------------------------------
# 保存
# ----------------------------------------------------------------------
def upsert(conn, item, now_iso):
    # 即決(buy-now)／落札相場(market)どちらのitemでも欠損キーが無いよう既定値で埋める。
    p = {
        "id": item.get("id"),
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "image": item.get("image", ""),
        "current_price": item.get("current_price"),
        "keyword": item.get("keyword", ""),
        "brand": item.get("brand", ""),
        "rank": item.get("rank", ""),
        "category": item.get("category", ""),
        "shape": item.get("shape", ""),
        "sold_date": item.get("sold_date", ""),
        "auction": item.get("auction", ""),
        "start_price": item.get("start_price"),
        "buy_now_price": item.get("buy_now_price"),
        "unit_price": item.get("unit_price"),
        "start_at": item.get("start_at", ""),
        "auction_round": item.get("auction_round", ""),
        "lane": item.get("lane", ""),
        "lot_position": item.get("lot_position", ""),
        "my_prebid": item.get("my_prebid"),
        "now": now_iso,
    }
    conn.execute(
        """
        INSERT INTO items (id, title, url, image, current_price, keyword,
                           brand, rank, category, shape, sold_date, auction,
                           start_price, buy_now_price, unit_price, start_at,
                           auction_round, lane, lot_position, my_prebid,
                           first_seen, last_seen)
        VALUES (:id, :title, :url, :image, :current_price, :keyword,
                :brand, :rank, :category, :shape, :sold_date, :auction,
                :start_price, :buy_now_price, :unit_price, :start_at,
                :auction_round, :lane, :lot_position, :my_prebid,
                :now, :now)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, url=excluded.url, image=excluded.image,
            current_price=excluded.current_price, keyword=excluded.keyword,
            brand=excluded.brand, rank=excluded.rank, category=excluded.category,
            shape=excluded.shape, sold_date=excluded.sold_date, auction=excluded.auction,
            start_price=excluded.start_price, buy_now_price=excluded.buy_now_price,
            unit_price=excluded.unit_price, start_at=excluded.start_at,
            auction_round=excluded.auction_round, lane=excluded.lane,
            lot_position=excluded.lot_position, my_prebid=excluded.my_prebid,
            last_seen=excluded.last_seen
        """,
        p,
    )


def load_keywords():
    out = []
    if os.path.exists(KEYWORDS_PATH):
        with open(KEYWORDS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
    return out


def collect(pages=PAGES_PER_KEYWORD, log=print):
    init_db()
    if not ensure_logged_in(log=log):
        log("ログインできないため収集を中止します。")
        return 0
    keywords = load_keywords()
    if not keywords:
        log("キーワードがありません（keywords.txt を確認してください）。")
        return 0

    now = now_jst()
    now_iso = now.isoformat()
    conn = get_db()
    total = 0
    pages_fetched = 0
    first = True
    # 鮮度掃除（buy-now）の対象キーワード＝今回そのキーワードの全ページを
    # エラーなく取得できたものだけ。途中取得失敗のキーワードは誤削除を防ぐため除外。
    fresh_keywords = []
    for kw in keywords:
        kw_count = 0
        raw_count = 0
        kw_ok = True   # このキーワードの試行ページが全てエラーなく取得できたか
        for page in range(1, pages + 1):
            try:
                _, page_html = fetch_search(kw, page=page)
                pages_fetched += 1
            except Exception as e:  # noqa: BLE001
                log(f"  取得失敗: {kw} (p{page}) -> {e}")
                kw_ok = False   # 途中ページ失敗 → 鮮度掃除の対象から外す（誤削除防止）
                break
            items = parse_search(page_html, kw, dump=first)
            first = False
            if not items:
                break
            raw_count += len(items)
            # ブランド＋商品名でAND絞り込み（OR検索の誤マッチ/他ブランドを除外）
            for it in items:
                if not match_keyword(it, kw):
                    continue
                upsert(conn, it, now_iso)
                kw_count += 1
                total += 1
            conn.commit()
            time.sleep(POLITE_DELAY)
        if kw_ok:
            fresh_keywords.append(kw)
        log(f"  「{kw}」 {kw_count}件（候補{raw_count}件から絞込）")

    # ログインは通ったのに解析0件＝一覧ページの構造に解析を合わせる必要あり
    warning = ""
    if pages_fetched > 0 and total == 0:
        src_label = "即決一覧(buy-now)" if SOURCE == "buynow" else "落札相場(market-prices)"
        warning = (
            f"ログインはできましたが{src_label}から商品を1件も解析できませんでした。"
            f"一覧ページの構造に合わせてパーサー調整が必要です（{os.path.basename(ACTIVE_SAMPLE_PATH)} を確認）。"
        )
        log("⚠ " + warning)

    # keywords.txt から外したキーワードの商品は掃除（一覧をリストと一致させる）
    if keywords:
        ph = ",".join("?" * len(keywords))
        conn.execute(f"DELETE FROM items WHERE keyword NOT IN ({ph})", keywords)
    # SOURCE切替時の残骸ガード：現行ソースと不整合な旧データを除く。
    # SOURCE=="buynow" のとき、即決固有データ（即決価格/開始価格/開催回）を一切持たず
    # 落札日だけを持つ＝旧・落札相場(market)の残骸を掃除する。正当な即決行は消さない。
    if SOURCE == "buynow":
        conn.execute(
            "DELETE FROM items "
            "WHERE buy_now_price IS NULL AND start_price IS NULL "
            "AND (auction_round IS NULL OR auction_round='') "
            "AND sold_date IS NOT NULL AND sold_date<>''"
        )
    # 即決(buy-now)の在庫鮮度：取得が正常完了したキーワードに限り、今回の収集で
    # 再確認されなかった行（last_seen が今回のタイムスタンプ now_iso と異なる行）を
    # 消す。売却済み・一覧から外れた即決商品が最大3日ダッシュボードに残るのを防ぐ。
    # 取得失敗キーワードは fresh_keywords に入れていないので消さない（一時的失敗で
    # データを失わないため）。market では適用しない（従来の3日ロジックのみ）。
    # warning が立つ＝解析が全滅（site構造変化/セッション切れ等で parse 0件）。その
    # ときは fetch 成功でも全行が last_seen 不一致になり全消ししてしまうため、鮮度掃除は
    # スキップして安全側の3日掃除に委ねる（一時的なサイト変化でデータを失わない）。
    if SOURCE == "buynow" and not warning:
        for kw in fresh_keywords:
            conn.execute(
                "DELETE FROM items WHERE keyword=? AND last_seen<>?",
                (kw, now_iso),
            )
    # 3日見かけない商品は掃除（buy-nowでは上の鮮度掃除でほぼ発火しないが保険として維持）
    seen_cut = (now - timedelta(days=3)).isoformat()
    conn.execute("DELETE FROM items WHERE last_seen < ?", (seen_cut,))
    set_meta(conn, "last_run", now_iso)
    set_meta(conn, "parse_warning", warning)
    conn.commit()
    conn.close()
    log(f"収集完了: {total}件")
    return total


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true", help="初回ログイン＆動作確認")
    ap.add_argument("--pages", type=int, default=PAGES_PER_KEYWORD)
    args = ap.parse_args()
    if args.setup:
        print("=== エコリング ログインテスト ===")
        if login():
            print("セッションを保存しました。続けてサンプル収集を行います …")
            init_db()
            kws = load_keywords()
            if kws:
                url, h = fetch_search(kws[0])
                its = parse_search(h, kws[0], dump=True)
                print(f"検索『{kws[0]}』→ 商品リンク {len(its)} 件を検出")
                print(f"サンプルHTMLを {ACTIVE_SAMPLE_PATH} に保存しました（解析精度の確認用）。")
                if its[:1]:
                    s = its[0]
                    print(f"例: id={s['id']} 価格={s['current_price']} 画像={'有' if s['image'] else '無'} 題={s['title'][:30]}")
    else:
        print(f"[{now_jst():%H:%M:%S}] 収集開始 …")
        collect(pages=args.pages)
