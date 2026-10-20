"""送信の状態の正本(SQLite)。「このフォームに、いま誰が何をしたか」をここ1か所で持つ。実行環境: Windows / Python 3.9+, 標準ライブラリのみ。

状態: 準備(予約した。まだ送信ボタンを押していない) → クリック開始(押した) → 結果(送信済み / 不明(要確認) / 要手動 など)。
- 同じフォームは1つの実行だけが確保できる(主キーの一意制約と BEGIN IMMEDIATE による)。入力URLと転送後のURLの両方を同じ取引で確保する。
- 「準備」のまま RECOVER_AFTER 秒を過ぎた予約は、押す前に止まったと言えるので、別の実行が引き継げる。
  「クリック開始」以降は、送れたか分からないので、自動では二度と送らない(再送は人が先方で確認してから)。
- 送信ログCSVは人が読む記録。二重送信を止める根拠はこの DB。
原理的に取りこぼす型: 相手のサーバー側には冪等キーが無いので、「必ず1回だけ届く」は保証できない。「不明のときは再送しない」を基本にして、確認は人が行う。
"""
import os
import re
import secrets
import sqlite3
import time
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import ng_list



def _default_db():
    """OneDrive などの同期フォルダの外に置く(同期中の SQLite は壊れうる)。LOCALAPPDATA が使えなければ 出力/ に置く"""
    base = os.environ.get("LOCALAPPDATA")
    if base and Path(base).is_dir():
        return Path(base) / "FormListTool" / "送信状態.db"
    return Path(__file__).resolve().parent / "出力" / "送信状態.db"


DB = _default_db()
CHECK_RUNNING = "checking"
CHECK_DONE = "done"
CHECK_FAILED = "failed"
CHECK_LEASE_SECONDS = 600
RECOVER_AFTER = 600  # 秒。「準備」のまま、この時間を過ぎた予約は引き継げる
PREPARED = "準備"


def _conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB), timeout=30, isolation_level=None)
    c.execute("CREATE TABLE IF NOT EXISTS sends (key TEXT PRIMARY KEY, url TEXT, company TEXT, state TEXT, memo TEXT, created REAL, updated REAL, owner TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS check_results (
        key TEXT PRIMARY KEY,
        url TEXT,
        company TEXT,
        status TEXT,
        attempts INTEGER,
        result_json TEXT,
        last_error TEXT,
        created REAL,
        updated REAL,
        lease_id TEXT
    )""")
    if "owner" not in {r[1] for r in c.execute("PRAGMA table_info(sends)")}:
        try:
            c.execute("ALTER TABLE sends ADD COLUMN owner TEXT")  # 旧い版で作られた表に持ち主の列を足す
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):  # 同時に起動した別の実行が先に足した場合だけ許す
                raise
    if "lease_id" not in {r[1] for r in c.execute("PRAGMA table_info(check_results)")}:
        try:
            c.execute("ALTER TABLE check_results ADD COLUMN lease_id TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
    return c


def keys_of(urls):
    """同じページの別の書き方(/index.html・/index.php など)を同じ鍵にする"""
    ks = (re.sub(r"/index\.(?:html?|php)$", "", k) for k in (ng_list.norm_url(u) for u in urls if u) if k)
    return list(dict.fromkeys(k for k in ks if k))


def _one_key(url):
    ks = keys_of([url])
    return ks[0] if ks else ""


def check_key(url, variant=""):
    """Cache key for read-only site checks. Keep meaningful query params.
    variant は「どの行に対する確認か」の指紋(formlist.row_fingerprint)。照合の判定は行ごとの名前・電話・住所で変わるので、
    URLが同じでも指紋が違えば別のキーにする(前回の別の値・別の店舗の判定を流さない)。v2 のキーは指紋が無かったので再利用しない"""
    if not url:
        return ""
    raw = url if str(url).startswith(("http://", "https://")) else "http://" + str(url)
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    host = (parts.netloc or "").lower().removeprefix("www.")
    path = re.sub(r"/index\.(?:html?|php)$", "", parts.path or "/").rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)), doseq=True)
    return "check:v3:" + urlunsplit((scheme, host, path, query, "")) + (("#" + variant) if variant else "")


def reserve(urls, company=""):
    """全てのURLを確保できたら、確保の持ち主を示す印(文字列)を返す。1つでも他の実行に確保されていれば、何も確保せず None。
    以後の start_click / mark には、この印を渡す(引き継がれた古い持ち主が、新しい持ち主の状態を書き換えないため)"""
    ks = keys_of(urls)
    if not ks:
        return None
    token = secrets.token_hex(8)
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        now = time.time()
        for k in ks:
            row = c.execute("SELECT state, updated FROM sends WHERE key=?", (k,)).fetchone()
            if row and not (row[0] == PREPARED and now - row[1] > RECOVER_AFTER):
                c.execute("ROLLBACK")
                return None
        for k in ks:
            c.execute("INSERT OR REPLACE INTO sends VALUES (?,?,?,?,?,?,?,?)", (k, k, company, PREPARED, "", now, now, token))
        c.execute("COMMIT")
        return token
    except sqlite3.Error:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        c.close()


def begin_check(url, company="", retry_limit=2, variant=""):
    """Reserve or resume a read-only site check. Returns (action, payload)."""
    key = check_key(url, variant)
    if not key:
        return "retry_limit", {"verdict": "取得失敗", "note": "empty url", "checkpoint_status": "retry_limit"}
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        now = time.time()
        row = c.execute("SELECT status, attempts, result_json, last_error, updated, lease_id FROM check_results WHERE key=?", (key,)).fetchone()
        if row and row[0] == CHECK_DONE and row[2]:
            c.execute("COMMIT")
            result = json.loads(row[2])
            result["checkpoint_status"] = "cached"
            return "cached", result
        if row and row[0] == CHECK_RUNNING and now - float(row[4] or 0) <= CHECK_LEASE_SECONDS:
            c.execute("COMMIT")
            return "running", {"verdict": "取得失敗", "note": "check already running", "checkpoint_status": "running"}
        attempts = int(row[1] or 0) if row else 0
        if attempts >= retry_limit:
            c.execute("COMMIT")
            result = json.loads(row[2]) if row and row[2] else {"verdict": "取得失敗", "note": row[3] if row else "retry limit reached"}
            result["checkpoint_status"] = "retry_limit"
            return "retry_limit", result
        attempts += 1
        lease_id = secrets.token_hex(16)
        if row:
            c.execute("UPDATE check_results SET url=?, company=?, status=?, attempts=?, updated=?, lease_id=? WHERE key=?",
                      (url, company, CHECK_RUNNING, attempts, now, lease_id, key))
        else:
            c.execute("""INSERT INTO check_results
                (key, url, company, status, attempts, result_json, last_error, created, updated, lease_id)
                VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?)""",
                      (key, url, company, CHECK_RUNNING, attempts, now, now, lease_id))
        c.execute("COMMIT")
        return "run", {"attempts": attempts, "lease_id": lease_id}
    except sqlite3.Error:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        c.close()


def save_check(url, company, result, lease_id=None, variant=""):
    key = check_key(url, variant)
    if not key or not lease_id:
        return False
    c = _conn()
    try:
        now = time.time()
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
        n = c.execute("""UPDATE check_results SET
                url=?,
                company=?,
                status=?,
                result_json=?,
                last_error='',
                updated=?
            WHERE key=? AND status=? AND lease_id=?
        """, (url, company, CHECK_DONE, payload, now, key, CHECK_RUNNING, lease_id)).rowcount
        return n == 1
    finally:
        c.close()


def fail_check(url, company, error, lease_id=None, variant=""):
    key = check_key(url, variant)
    if not key or not lease_id:
        return False
    c = _conn()
    try:
        n = c.execute("""UPDATE check_results SET company=?, status=?, last_error=?, updated=?
            WHERE key=? AND status=? AND lease_id=?""",
                      (company, CHECK_FAILED, str(error)[:200], time.time(), key, CHECK_RUNNING, lease_id)).rowcount
        return n == 1
    finally:
        c.close()


def start_click(urls, token):
    """送信ボタンを押す直前に呼ぶ。自分の確保がまだ「準備」のまま残っているときだけ True。
    確保が引き継がれていた(10分以上かかった)・消されていた場合は False=押さない"""
    ks = keys_of(urls)
    if not ks or not token:
        return False
    c = _conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        n = 0
        for k in ks:
            n += c.execute("UPDATE sends SET state=?, updated=? WHERE key=? AND state=? AND owner=?", ("クリック開始", time.time(), k, PREPARED, token)).rowcount
        if n != len(ks):
            c.execute("ROLLBACK")
            return False
        c.execute("COMMIT")
        return True
    except sqlite3.Error:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        c.close()


def mark(urls, state, memo="", token=None):
    """結果を記録する。token を渡したときは、自分の確保のものだけを書き換える"""
    ks = keys_of(urls)
    if not ks:
        return
    c = _conn()
    try:
        for k in ks:
            if token:
                c.execute("UPDATE sends SET state=?, memo=?, updated=? WHERE key=? AND owner=?", (state, memo[:200], time.time(), k, token))
            else:
                c.execute("UPDATE sends SET state=?, memo=?, updated=? WHERE key=?", (state, memo[:200], time.time(), k))
    finally:
        c.close()


def blocked_keys():
    """自動では送らないフォームのキー(「準備」で止まったものは含めない)"""
    if not DB.exists():
        return set()
    c = _conn()
    try:
        return {r[0] for r in c.execute("SELECT key FROM sends WHERE state != ?", (PREPARED,))}
    finally:
        c.close()
