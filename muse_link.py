"""Muse(利用者自身の AI)とつなぐ部品。方式A(D-20261111-006)の PC 側。実行環境: Windows / Mac / Linux(Python 3.9+)

動き: app.py のバックグラウンドスレッドとして、こちらのリモート MCP サーバーへ外向きに接続し(内向きのポートは開けない)、
  受け取った道具の呼び出しを、mcp_server.py と同じ内部 API(127.0.0.1 の /api/*、X-Token つき)へ中継して、結果を返す。
  中継先を内部 API にそろえるので、確認ゲート・承認ダイアログとチケット・二重送信防止はエンジンによらず同じに効く。
  この部品は承認を肩代わりしない(/api/approve は中継しない。送信は app.py 側の承認が無ければ始まらない)。
いつ接続するか: AI設定.json の engine が muse のときだけ。claude_code の間は外向きの通信を一切しない。
接続先: SERVER_URL(サーバー側の設置で決まる。空の間は接続せず「接続先がまだ用意されていません」と出す)。
  試験・運用者は環境変数 FORMLIST_MUSE_URL で指定できる。
サーバーとの取り決め(仮。サーバー側 S1 の設計で確定させる。変わったらここと _poll/_report を直す):
  POST <URL>/poll   見出し Authorization: Bearer <許可キー>、本文 {"install_id","version"}。
     200 {"calls":[{"id","method","path","body","expires_at"}], "muse_linked": true/false} / 204 呼び出しなし / 401・403 キーの拒否
  POST <URL>/result 本文 {"install_id","id","status","code","body"}
切断中の指示は保持しない: expires_at(数値・今から MAX_TTL 秒以内)が無い・数値でない・遠すぎる呼び出しは実行せず refused、
  過ぎたものは expired と返す。同じ id は1回だけ実行する(2回目は duplicate)。実行した id は期限まで done_path() のファイルに残し、
  再起動しても効かせる。件数では捨てず、期限を過ぎた id だけを捨てる(期限切れの呼び出しは実行されないので再実行は起きない)。
  記録のファイルが壊れていたら、その時刻から MAX_TTL のあいだは実行しない(壊れた記録にあった id を再実行しないため)。
接続先は https だけ。例外は http://127.0.0.1(試験用の偽サーバー。同じPCの中)。中継・問い合わせとも転送(リダイレクト)には従わない。
キーの実値は、ログ・応答・状態のどこにも出さない(license_check.py と同じ方針)。

陽性対照(test_muse_link.py): 許可リスト外(/api/approve・/api/quit・/api/ai_engine)は中継されない / 同じ id の2回目は実行されない /
  期限切れは実行されない / 401 で denied になる / engine が claude_code なら接続を始めない。
原理的に取りこぼす型: サーバー側が別人の呼び出しを渡してきた場合の結び間違いは、PC 側では見分けられない(サーバー側の台帳照合で防ぐ)。
  実回線(NAT の内側・プロキシ)での到達は試験していない(実証 V3 で確かめる)。
"""
import json
import os
import threading
import time
from urllib.parse import urlparse

import requests

import ai_engine
import state

SERVER_URL = ""  # リモート MCP サーバーの入口。サーバー側(S1)の設置で決まる。空の間は接続しない
URL_ENV = "FORMLIST_MUSE_URL"
POLL_TIMEOUT = 40  # サーバー側が呼び出しを待つ時間(長いポーリング)より少し長く
RELAY_TIMEOUT = 60
BACKOFF = (2, 4, 8, 16, 32, 64, 120, 300)  # つながらないときの待ち(秒)。指数的に空けて、最大5分
DENIED_WAIT = 600  # キーの拒否のあとは、10分より短い間隔では問い直さない
LOG_MAX_ROWS = 100  # 暫定値(ガイア判断 2026-11-11)。中継の応答をここで切り詰める。道具側(formlist_log の limit)の切り詰めとは別
LOG_MAX_DAYS = 30  # 暫定値(同上)
META_KEEP = ("sender", "problems", "messages", "message_info")  # 中継で返す /api/meta の項目(相談470 (A))。残りの15項目(copy を含む)は返さない
META_SENDER_KEEP = ("会社名", "氏名", "メール", "住所", "署名")
TARGETS_MAX = 100  # 中継で返す送る内容の社数の上限(相談470 (B))
# 足さないと決めた読み取り(相談470 (B)。道具が実際に失敗する証拠が出てから足す)と、そのときの暫定の上限:
#   /api/results 200行まで・/api/openings 1リスト200社まで・/api/manual_kit 50社まで・/api/report の days 30まで
MAX_TTL = 3600  # 期限は今から1時間以内のものだけ受ける(古い指示の遅延実行を防ぎ、実行済みの記録も1時間で捨てられる)
# 中継してよい内部 API(mcp_server.py が使うものから、承認・終了・更新・エンジン切り替えを除いたもの)。
# ガイアの判断(2026-11-11 相談468): 送信元の書き込み(/api/sender。9項目・資料URL・計測キー)は本人の画面からだけ。
#   送信元の過去の値も返す設定の履歴(/api/settings/history)は、戻す用途が無くなったので読み取りも外す。
#   送信ログ(/api/log)は読み取りのみで、最新 LOG_MAX_ROWS 件かつ直近 LOG_MAX_DAYS 日の両方を満たす行だけ(_limit_log)。
#   ローカルのホスト(Claude Code 等)が mcp_server.py から直接つなぐ経路はこの一覧を通らないので、権限は変わらない。
ALLOWED = {
    ("GET", "/api/job"), ("GET", "/api/license"), ("GET", "/api/unsure"), ("GET", "/api/ngscreen"), ("GET", "/api/meta"),
    ("GET", "/api/message/patterns"), ("GET", "/api/opens"), ("GET", "/api/log"), ("GET", "/api/targets"),
    ("POST", "/api/search"), ("POST", "/api/ng"), ("POST", "/api/ng/undo"),
    ("POST", "/api/message"), ("POST", "/api/message/move"), ("POST", "/api/message/delete"), ("POST", "/api/message/pattern"),
    ("POST", "/api/sample"), ("POST", "/api/openings"), ("POST", "/api/send"), ("POST", "/api/outcome"), ("POST", "/api/reply"),
    ("POST", "/api/manual"), ("POST", "/api/manual_sent"), ("POST", "/api/bug_report"), ("POST", "/api/bug_report/delete"),
}
MESSAGES = {
    "off": "",
    "no_server": "Muse との接続先がまだ用意されていません。画面の操作はこのまま使えます",
    "bad_url": "Muse との接続先の設定が正しくありません。担当者にご連絡ください",
    "no_key": "許可キーが入っていないので、Muse とつなげません。担当者にご連絡ください",
    "connecting": "つなぎ直しています",
    "connected": "Muse とつながっています",
    "not_linked": "Muse の画面での接続がまだです。Muse の画面で接続してください",
    "offline": "Muse につながっていません。インターネットの接続を確かめてください。このまま画面の操作では使えます",
    "denied": "キーが合っていないようです。担当者にご連絡ください",
}

_LOCK = threading.Lock()
_STATE = {"state": "off", "last_ok_at": None, "since": None}
_RUN = {"stop": None, "thread": None, "args": None}
_DONE = {}  # 実行した id → その呼び出しの期限(done_path() のファイルと同じ中身)
_DONE_FILE = {"loaded": False, "blocked_until": 0.0}


def url():
    return (os.environ.get(URL_ENV) or SERVER_URL).rstrip("/")


def url_ok(base):
    """https だけ通す。http は 127.0.0.1(試験用の偽サーバー)だけ。許可キーを平文で外へ送らない"""
    u = urlparse(base)
    if u.scheme == "https" and u.hostname:
        return True
    return u.scheme == "http" and u.hostname == "127.0.0.1"


def done_path():
    return state.DB.parent / "muse_link_done.json"


def _load_done(now):
    """_LOCK の中で呼ぶ。初回だけファイルから読み、期限を過ぎた id を捨てる"""
    if not _DONE_FILE["loaded"]:
        p = done_path()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ValueError("形が違う")
            _DONE.update({str(k): float(v) for k, v in d.items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            try:
                _DONE_FILE["blocked_until"] = p.stat().st_mtime + MAX_TTL
            except OSError:
                _DONE_FILE["blocked_until"] = now + MAX_TTL
        _DONE_FILE["loaded"] = True
    for k in [k for k, v in _DONE.items() if v < now]:
        del _DONE[k]


def _save_done():
    p = done_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_DONE, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _set(state):
    with _LOCK:
        if _STATE["state"] != state:
            _STATE["since"] = time.time()
        _STATE["state"] = state
        if state == "connected":
            _STATE["last_ok_at"] = time.time()


def status():
    """画面・whoami・サポート報告が見る状態。キーの値は含めない"""
    with _LOCK:
        d = dict(_STATE)
    d["message"] = MESSAGES.get(d["state"], "")
    return d


def relay(call, port, token, session=None, now=None):
    """1件の呼び出しを内部 API へ中継する。戻りは {"id","status","code","body"}。status: ok / refused / duplicate / expired / error"""
    cid = str(call.get("id") or "")
    method = str(call.get("method") or "").upper()
    path = str(call.get("path") or "")
    out = {"id": cid, "status": "refused", "code": None, "body": None}
    if not cid:
        out["body"] = {"error": "id がありません"}
        return out
    now = time.time() if now is None else now
    exp = call.get("expires_at")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp > now + MAX_TTL:
        out["body"] = {"error": "期限(expires_at)が無いか、正しくない呼び出しは実行しません"}
        return out
    if now > exp:
        out["status"] = "expired"
        return out
    pu = urlparse(path)
    if pu.scheme or pu.netloc or (method, pu.path) not in ALLOWED:
        out["body"] = {"error": "この操作は Muse からは受け付けていません"}
        return out
    with _LOCK:
        _load_done(now)
        if now < _DONE_FILE["blocked_until"]:
            out.update(status="error", body={"error": "実行の記録が壊れていたため、しばらく Muse からの操作を止めています"})
            return out
        if cid in _DONE:
            out["status"] = "duplicate"
            return out
        _DONE[cid] = float(exp)
        try:
            _save_done()  # 実行の前に残す(実行の途中で落ちても、同じ id を2回実行しない)
        except OSError:
            del _DONE[cid]
            out.update(status="error", body={"error": "実行の記録を残せないので実行しません"})
            return out
    s = session or _local_session()
    try:
        r = s.request(method, f"http://127.0.0.1:{port}{path}", headers={"X-Token": token},
                      json=call.get("body") if method == "POST" else None, timeout=RELAY_TIMEOUT,
                      allow_redirects=False)  # 転送先へ合言葉(X-Token)を渡さない
        try:
            body = r.json()
        except ValueError:
            body = {"error": r.text[:200]}
        code = r.status_code
        if (method, pu.path) == ("GET", "/api/log"):
            body = _limit_log(body, now)
        elif (method, pu.path) == ("GET", "/api/meta"):
            body = _limit_meta(body)
        elif (method, pu.path) == ("GET", "/api/targets"):
            code, body = _limit_targets(code, body)
        out.update(status="ok", code=code, body=body)
    except requests.RequestException as e:
        out.update(status="error", body={"error": f"ツールの内部につながりませんでした({type(e).__name__})"})
    return out


def _row_time(row):
    """送信ログの「日時」(例: 2026-10-07 15:09)を時刻にする。読めなければ None(その行は外へ出さない)"""
    s = str(row.get("日時") or "").strip() if isinstance(row, dict) else ""
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return time.mktime(time.strptime(s[:n], fmt))
        except ValueError:
            continue
    return None


def _limit_log(rows, now):
    """中継で返す送信ログを、直近 LOG_MAX_DAYS 日の行のうち新しい LOG_MAX_ROWS 件だけにする(古い順のまま)。一覧でなければ空にする"""
    if not isinstance(rows, list):
        return []
    since = now - LOG_MAX_DAYS * 86400
    timed = [(t, i, r) for i, r in enumerate(rows) for t in [_row_time(r)] if t is not None and since <= t <= now + 86400]
    timed.sort(key=lambda x: (x[0], x[1]))
    return [r for _, _, r in timed[-LOG_MAX_ROWS:]]


def _limit_meta(body):
    """中継で返す /api/meta を、道具が使う項目だけにする(ガイア判断 2026-11-11 相談470 (A))。
    道具ごとに使う項目(mcp_server.py の実読): formlist_sender_get=sender・problems / formlist_message_get=messages・message_info /
    formlist_preview=sender の 会社名・氏名・メール と、法定表示の検査(send_rules.legal_missing)が見る 住所・署名。"""
    if not isinstance(body, dict):
        return {}
    out = {k: body[k] for k in META_KEEP if k in body}
    if isinstance(out.get("sender"), dict):
        out["sender"] = {k: v for k, v in out["sender"].items() if k in META_SENDER_KEEP}
    return out


def _limit_targets(code, body):
    """中継で返す /api/targets(送る内容)。送る会社が TARGETS_MAX 社を超えるときは、切り詰めずに断る(見せていない会社へ送ることにならないように)。
    送らない会社の一覧は TARGETS_MAX 件までにし、全体の数を skipped_total に入れる"""
    if not isinstance(body, dict):
        return code, body
    rows = body.get("sendable")
    if isinstance(rows, list) and len(rows) > TARGETS_MAX:
        return 400, {"error": f"送る会社が{len(rows)}社あり、Muse からは{TARGETS_MAX}社までしか全文を出せません。送る社数を{TARGETS_MAX}以下にするか、ツールの画面で確かめてください"}
    sk = body.get("skipped")
    if isinstance(sk, list) and len(sk) > TARGETS_MAX:
        body = dict(body, skipped=sk[:TARGETS_MAX], skipped_total=len(sk))
    return code, body


def _local_session():
    s = requests.Session()
    s.trust_env = False  # 127.0.0.1 への中継にプロキシの設定を使わない
    return s


def poll_once(key, iid, version, port, token, stop=None, http=None, local=None):
    """1回だけサーバーへ問い合わせて、来た呼び出しを中継する。戻りは状態(MESSAGES のキー)"""
    base = url()
    if not base:
        return "no_server"
    if not url_ok(base):
        return "bad_url"
    if not key:
        return "no_key"
    http = http or requests
    try:
        r = http.post(base + "/poll", headers={"Authorization": f"Bearer {key}"},
                      json={"install_id": iid, "version": version}, timeout=POLL_TIMEOUT, allow_redirects=False)
    except requests.RequestException:
        return "offline"
    if r.status_code in (401, 403):
        return "denied"
    if r.status_code == 204:
        return "connected"
    if r.status_code != 200:
        return "offline"
    try:
        d = r.json()
    except ValueError:
        return "offline"
    if not isinstance(d, dict):
        return "offline"
    for call in d.get("calls") or []:
        if stop is not None and stop.is_set():  # claude_code へ戻した後は、受け取っていた分も実行しない
            break
        if not isinstance(call, dict):
            continue
        res = relay(call, port, token, session=local)
        try:
            http.post(base + "/result", headers={"Authorization": f"Bearer {key}"}, json={"install_id": iid, **res}, timeout=POLL_TIMEOUT, allow_redirects=False)
        except requests.RequestException:
            return "offline"
    return "not_linked" if d.get("muse_linked") is False else "connected"


def _loop(stop, port, token, version, key_reader, iid_reader):
    fails = 0
    local = _local_session()
    while not stop.is_set():
        if ai_engine.engine() != "muse":
            break
        try:
            st = poll_once(key_reader(), iid_reader(), version, port, token, stop=stop, local=local)
        except Exception:
            st = "offline"
        if stop.is_set():
            break
        _set(st)
        if st in ("connected", "not_linked"):
            fails = 0
            wait = 1 if st == "connected" else 30  # サーバーがすぐ返しても、問い合わせが詰まって回らないように1秒は空ける
        elif st == "denied":
            wait = DENIED_WAIT
        elif st in ("no_server", "bad_url"):
            wait = DENIED_WAIT
        else:
            wait = BACKOFF[min(fails, len(BACKOFF) - 1)]
            fails += 1
        if wait:
            stop.wait(wait)
    with _LOCK:
        current = _RUN["stop"] is stop
    if current:  # つなぎ直しで新しいスレッドが動いているときは、その状態を上書きしない
        _set("off")


def start(port, token, version, key_reader, iid_reader):
    """engine が muse のときだけ接続のスレッドを始める。claude_code なら何もしない(外向きの通信をしない)。戻りはスレッドか None"""
    if ai_engine.engine() != "muse":
        _set("off")
        return None
    with _LOCK:
        t = _RUN["thread"]
        if t is not None and t.is_alive() and not _RUN["stop"].is_set():
            return t
        stop = threading.Event()
        _RUN.update(stop=stop, args=(port, token, version, key_reader, iid_reader))
    _set("connecting")
    t = threading.Thread(target=_loop, args=(stop,) + _RUN["args"], name="muse-link", daemon=True)
    with _LOCK:
        _RUN["thread"] = t
    t.start()
    return t


def stop():
    """接続を閉じる(claude_code へ戻したとき)。待ちの途中なら、その場で抜ける"""
    with _LOCK:
        ev = _RUN["stop"]
    if ev is not None:
        ev.set()
    _set("off")


def apply(port, token, version, key_reader, iid_reader):
    """保存の直後に呼ぶ。muse なら(つなぎ直して)始め、claude_code なら閉じる"""
    stop()
    return start(port, token, version, key_reader, iid_reader)
