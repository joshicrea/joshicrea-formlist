"""Muse(利用者自身の AI)とつなぐ部品。方式A(D-20261111-006)の PC 側。実行環境: Windows / Mac / Linux(Python 3.9+)

動き: app.py のバックグラウンドスレッドとして、こちらのリモート MCP サーバーへ外向きに接続し(内向きのポートは開けない)、
  受け取った道具の呼び出しを、mcp_server.py と同じ内部 API(127.0.0.1 の /api/*、X-Token つき)へ中継して、結果を返す。
  中継先を内部 API にそろえるので、確認ゲート・承認ダイアログとチケット・二重送信防止はエンジンによらず同じに効く。
  この部品は承認を肩代わりしない(/api/approve は中継しない。送信は app.py 側の承認が無ければ始まらない)。
いつ接続するか: AI設定.json の engine が muse のときだけ。claude_code の間は外向きの通信を一切しない。
接続先: SERVER_URL = 本番の入口 https://lunettes.co.jp/lic/mcp(2026-11-16 20:07 にこの入口の /poll.php へ POST し、鍵なしで401・開発専用キーで200を確認)。空にすると接続せず「接続先がまだ用意されていません」と出す。
  試験・運用者は環境変数 FORMLIST_MUSE_URL で指定できる。
サーバーとの取り決め(S1 で確定・ガイア相談45 2026-11-16。変わったらここと poll_once を直す):
  POST <URL>/poll.php   見出し X-License-Key: <許可キー>、本文 {"install_id","version"}。
     200 {"calls":[{"id","method","path","body","expires_at"}], "muse_linked": true/false} / 204 呼び出しなし / 401・403 キーの拒否
  POST <URL>/result.php 見出し X-License-Key、本文 {"install_id","id","status","code","body"}
同じ操作の二度実行の防止(弱点4・波3): Muse が時間切れのあとに呼び直すと、サーバーは新しい id を付けるので、id の照合では止まらない。
  そこで、やり直すと効果が重なる操作(REPEAT_GUARDED: 本番送信・会社探し・手で送る・手で送った記録・返信の記録・不具合の提出)は、
  同じ操作(repeat_key: 道・引数をそろえた指紋)を、前回が「実行中」か「結果が分からない」あいだ(最長 REPEAT_WINDOW 秒)だけ止め、
  duplicate として、いつ受けたか・何が分からないか・いつから呼び直せるかを日本語で返す(破壊レポート1 弱点9)。
  前回の結果を受け取れた(内部 API が応答を返した。2xx でも 4xx でも)ときは、次の同じ操作を通す。「残りを送って」「同じ条件で探し直して」の
  正しい繰り返しを止めないため。本番送信は、内部 API 側で1回ごとに人の承認(画面のダイアログ)が要り、動いている間は 409 で断り、
  送り済みの会社は送信状態の記録で外すので、終わった後の呼び直しで同じ会社へ2通目は行かない(サーバー側にも送信の指紋の照合がある)。
  結果が分からない(内部 API が応答を返す前に切れた・5xx・時間切れ)ときは、実行されたかもしれないので止める(弱点8)。
  記録を外して呼び直しを通すのは、接続そのものが確立しなかったとき(ConnectTimeout・NewConnectionError)だけ。
  4xx の意味(実行の前に断ったか)には頼らない。4xx も「結果を受け取れた」として、2xx と同じに扱う(弱点10)。
  記録は repeat_path()(実行済み id のファイルの隣)に残し、再起動しても効かせる。読み取り(GET・見本・送信の下見)は何度でも実行する。
  値を置き換えるだけの操作(文面・送らない会社・書き出し・結果の印)は2回実行しても同じ状態になり、「入れる→戻す→入れる」の正しい
  繰り返しを止めてしまうので対象にしない。
  指紋のそろえ方(弱点7・迷ったら同じとみなして止める側): 道は大文字小文字と重ねた / をそろえる。本文は、空の欄(None・""・[]・{})を捨て、
  文字の数("10")は数(10)に、文字は前後の空白を除いて NFKC、一覧は並びを問わない(並べ替え)、file は場所を除いたファイル名(大文字小文字を問わない)。
  本番送信は送る中身を決める欄(REPEAT_FIELDS)だけを見て、send・confirm・知らない欄は捨てる。
受け取る呼び出しの大きさ(弱点3・波3): 道と本文の合計が MAX_CALL_BYTES を超える呼び出しは実行せず refused と返す。
切断中の指示は保持しない: expires_at(数値・今から MAX_TTL 秒以内)が無い・数値でない・遠すぎる呼び出しは実行せず refused、
  過ぎたものは expired と返す。同じ id は1回だけ実行する(2回目は duplicate)。実行した id は期限まで done_path() のファイルに残し、
  再起動しても効かせる。件数では捨てず、期限を過ぎた id だけを捨てる(期限切れの呼び出しは実行されないので再実行は起きない)。
  記録のファイルが壊れていたら、その時刻から MAX_TTL のあいだは実行しない(壊れた記録にあった id を再実行しないため)。
接続先は https だけ。例外は http://127.0.0.1(試験用の偽サーバー。同じPCの中)。中継・問い合わせとも転送(リダイレクト)には従わない。
キーの実値は、ログ・応答・状態のどこにも出さない(license_check.py と同じ方針)。

陽性対照(test_muse_link.py): 許可リスト外(/api/approve・/api/quit・/api/ai_engine)は中継されない / 同じ id の2回目は実行されない /
  期限切れは実行されない / 401 で denied になる / engine が claude_code なら接続を始めない /
  実行中・結果不明のあいだに別の id で同じ送信(並びや数の書き方を変えたものも)を呼び直しても実行されない(再起動後も) /
  送ったあとの切断(RemoteDisconnected)・5xx・時間切れのあとの呼び直しは止まる / 大きすぎる本文は実行されない / 鍵は X-License-Key で .php に送る。
誤爆の対照: 同じ読み取りは何度でも実行される / 引数が違う送信は実行される / 下見(send=false)は何度でも実行される /
  結果を受け取れた(2xx・4xx)あとの同じ送信・同じ会社探しは通る / 接続できなかった(相手が待ち受けていない)あとの呼び直しは通る。
原理的に取りこぼす型: サーバー側が別人の呼び出しを渡してきた場合の結び間違いは、PC 側では見分けられない(サーバー側の台帳照合で防ぐ)。
  実回線(NAT の内側・プロキシ)での到達は試験していない(実証 V3 で確かめる)。
"""
import hashlib
import json
import os
import posixpath
import re
import threading
import time
import unicodedata
from urllib.parse import parse_qsl, unquote, urlparse

import requests

import ai_engine
import state

SERVER_URL = "https://lunettes.co.jp/lic/mcp"  # リモート MCP サーバー(S1)の本番の入口。/poll.php と /result.php を足して使う。2026-11-16 20:07 に /poll.php へ POST: 鍵なし401・開発専用キー200。試験は conftest.py が空にして本番へ通信しない
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
MAX_CALL_BYTES = 64 * 1024  # 受け取る1件の呼び出し(道+本文の JSON)の上限。サーバーの Muse 入口(index.php)が受ける本文の上限と同じ
REPEAT_WINDOW = 600  # 同じ操作を1回だけにする時間(秒)。サーバーの待ち行列が呼び出しを持つ時間(10分)と同じ
REPEAT_BODY_MAX = 8 * 1024  # 2回目に返す「前回の結果」の本文を記録に残す上限。超える分は残さない(結果の要約だけ返す)
# やり直すと効果が重なる操作。POST /api/send は send が真のとき(本番送信)だけ。下見(send が偽)は読み取りとして何度でも実行する
REPEAT_GUARDED = {"/api/send", "/api/search", "/api/manual", "/api/manual_sent", "/api/reply", "/api/bug_report"}
# 指紋に入れる欄。ここに道があるときは、この欄だけを見る(知らない欄・send・confirm は捨てる)。本番送信の欄は app.py send_api と
# mcp_server.formlist_send が送る本文(file・max・supp・send・confirm)から。送る中身を決めない send・confirm・preview は入れない
REPEAT_FIELDS = {"/api/send": ("file", "max", "supp", "industry", "template_parts", "use_industry")}
_NUM = re.compile(r"[+-]?\d+(\.\d+)?")
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
_REPEAT = {}  # 操作の指紋 → {"at","id","code","body","done"}(repeat_path() のファイルと同じ中身)
_REPEAT_FILE = {"loaded": False, "blocked_until": 0.0}


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


def repeat_path():
    return done_path().with_name("muse_link_repeat.json")


def _load_repeat(now):
    """_LOCK の中で呼ぶ。初回だけファイルから読み、REPEAT_WINDOW を過ぎた記録を捨てる。壊れていたら、その時刻から REPEAT_WINDOW は対象の操作を止める"""
    if not _REPEAT_FILE["loaded"]:
        p = repeat_path()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ValueError("形が違う")
            for k, v in d.items():
                if isinstance(v, dict) and isinstance(v.get("at"), (int, float)) and not isinstance(v.get("at"), bool):
                    _REPEAT[str(k)] = v
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            try:
                _REPEAT_FILE["blocked_until"] = p.stat().st_mtime + REPEAT_WINDOW
            except OSError:
                _REPEAT_FILE["blocked_until"] = now + REPEAT_WINDOW
        _REPEAT_FILE["loaded"] = True
    for k in [k for k, v in _REPEAT.items() if v["at"] + REPEAT_WINDOW <= now]:
        del _REPEAT[k]


def _save_repeat():
    p = repeat_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_REPEAT, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _canon(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _norm_path(p):
    """道の書き方をそろえる: %xx を戻し、\\ を / に、重ねた / と . を畳み、末尾の / を除き、大文字小文字を問わない"""
    p = posixpath.normpath("/" + unquote(p).replace("\\", "/").lstrip("/"))  # 先頭の // は normpath が残すので先に外す
    return p.rstrip("/").casefold() or "/"


def _norm(v, key=None):
    """引数の書き方の違いを吸収する(迷ったら同じとみなす側)。空の欄は None を返し、呼び出し側で捨てる"""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else float(v)
    if isinstance(v, str):
        s = unicodedata.normalize("NFKC", v).strip()
        if s == "":
            return None
        if key == "file":  # 場所は除いてファイル名だけ。大文字小文字を問わない(Windows のファイル名と同じ)
            return s.replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if _NUM.fullmatch(s):
            f = float(s)
            return int(f) if f.is_integer() else f
        return s
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            n = _norm(x, str(k))
            if n is not None:
                out[unicodedata.normalize("NFKC", str(k)).strip()] = n
        return out or None
    if isinstance(v, (list, tuple)):
        items = [n for n in (_norm(x, key) for x in v) if n is not None]
        return sorted(items, key=_canon) or None  # 会社・業種の一覧の並びに意味は無い
    return str(v)


def repeat_key(method, path, body):
    """やり直すと効果が重なる操作なら、その指紋(書き方の違いをそろえた道・引数が同じなら同じ値)。読み取り・値の置き換えは None"""
    pu = urlparse(path)
    p = _norm_path(pu.path)
    if method != "POST" or p not in REPEAT_GUARDED:
        return None
    if p == "/api/send" and not (isinstance(body, dict) and body.get("send")):
        return None  # 下見(send が偽)は何も送らないので、何度でも実行する
    b = _norm(body)
    if p in REPEAT_FIELDS and isinstance(b, dict):
        b = {k: b[k] for k in REPEAT_FIELDS[p] if k in b}
    q = _norm({k: v for k, v in parse_qsl(pu.query, keep_blank_values=True)})
    return hashlib.sha256(_canon([method, p, q, b]).encode("utf-8")).hexdigest()


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
    body_in = call.get("body")
    try:
        size = len(path.encode("utf-8")) + len(_canon(body_in).encode("utf-8"))
    except (TypeError, ValueError):
        out["body"] = {"error": "呼び出しの内容が読めないので実行しません"}
        return out
    if size > MAX_CALL_BYTES:
        out["body"] = {"error": f"呼び出しの内容が大きすぎるので実行しません(上限 {MAX_CALL_BYTES // 1024}KB)"}
        return out
    fp = repeat_key(method, path, body_in)
    with _LOCK:
        _load_done(now)
        if now < _DONE_FILE["blocked_until"]:
            out.update(status="error", body={"error": "実行の記録が壊れていたため、しばらく Muse からの操作を止めています"})
            return out
        if cid in _DONE:
            out["status"] = "duplicate"
            return out
        if fp is not None:
            _load_repeat(now)
            if now < _REPEAT_FILE["blocked_until"]:
                out.update(status="error", body={"error": "同じ操作の記録が壊れていたため、しばらく送信・会社探しなどを Muse から止めています"})
                return out
        _DONE[cid] = float(exp)
        try:
            _save_done()  # 実行の前に残す(実行の途中で落ちても、同じ id を2回実行しない)
        except OSError:
            del _DONE[cid]
            out.update(status="error", body={"error": "実行の記録を残せないので実行しません"})
            return out
        if fp is not None:
            prev = _REPEAT.get(fp)
            if prev is not None and _rec_state(prev) != "finished":  # 前回が実行中か結果不明(時間切れのあとの呼び直し)。実行せず、分かっていることを返す
                out.update(status="duplicate", code=prev.get("code"), body=_duplicate_body(prev, pu.path))
                return out
            _REPEAT[fp] = {"at": float(now), "id": cid, "code": None, "body": None, "state": "running"}
            try:
                _save_repeat()  # 実行の前に残す(実行の途中で落ちても、呼び直しで2回実行しない)
            except OSError:
                del _REPEAT[fp]
                out.update(status="error", body={"error": "実行の記録を残せないので実行しません"})
                return out
    not_connected = False
    try:
        not_connected = _relay_http(out, session, method, path, pu, port, token, body_in, now)
    finally:
        if fp is not None:
            _finish_repeat(fp, cid, out, not_connected)
    return out


def _rec_state(rec):
    """記録の状態: running(実行中・または実行の途中で落ちた) / unknown(結果が分からない) / finished(内部 API の応答を受け取れた)。
    旧形式(done)は、done が真なら finished、偽なら unknown"""
    st = rec.get("state")
    if st in ("running", "unknown", "finished"):
        return st
    return "finished" if rec.get("done") else "unknown"


def _hhmm(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def _duplicate_body(prev, path):
    at = float(prev.get("at") or 0)
    until = at + REPEAT_WINDOW
    why = "まだ終わっていません(実行中か、実行の途中でツールが止まりました)" if _rec_state(prev) == "running" else \
        "途中で接続が切れるなどして、結果が分かっていません(実行されたかもしれません)"
    note = (f"同じ内容の操作を {_hhmm(at)} に受けて実行を始めましたが、{why}。二重に実行しないよう、今回は実行していません。"
            f"進み具合と結果は formlist_job と formlist_log で確かめてください。{_hhmm(until)} を過ぎれば、同じ操作をもう一度呼べます。")
    if urlparse(path).path == "/api/send":
        note += "送信は1回ごとに画面での承認が要り、すでに送った会社にはツールが二重には送りません。"
    return {"note": note, "duplicate_of": prev.get("id"), "previous_state": _rec_state(prev), "previous_at": _hhmm(at),
            "retry_after": _hhmm(until), "previous": prev.get("body")}


def _finish_repeat(fp, cid, out, never_connected):
    """実行の結果を同じ操作の記録に残す。
    接続そのものが確立しなかった(要求が相手に届いていない)ときだけ、記録を外して呼び直しを通す。
    内部 API の応答を受け取れた(5xx を除く。4xx も含む)ときは finished: 次の同じ操作を通す。
    それ以外(送ったあとの切断・時間切れ・5xx・その他の例外)は unknown: REPEAT_WINDOW のあいだ止める"""
    code = out.get("code")
    with _LOCK:
        rec = _REPEAT.get(fp)
        if rec is None or rec.get("id") != cid:
            return
        if never_connected:
            del _REPEAT[fp]
        else:
            b = out.get("body")
            try:
                keep = b if len(_canon(b).encode("utf-8")) <= REPEAT_BODY_MAX else {"note": "前回の結果は長いので残していません"}
            except (TypeError, ValueError):
                keep = None
            got = out.get("status") == "ok" and isinstance(code, int) and code < 500
            rec.update(code=code, body=keep, state="finished" if got else "unknown")
            rec.pop("done", None)
        try:
            _save_repeat()
        except OSError:
            pass  # 残せなくても、実行前に残した記録(running)が呼び直しを止める側に働く(壊れた側は止める)


def _relay_http(out, session, method, path, pu, port, token, body_in, now):
    s = session or _local_session()
    try:
        r = s.request(method, f"http://127.0.0.1:{port}{path}", headers={"X-Token": token},
                      json=body_in if method == "POST" else None, timeout=RELAY_TIMEOUT,
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
        return _never_connected(e)
    return False


def _never_connected(e):
    """接続の確立に失敗した(要求が相手に届いていない)と言えるときだけ真。requests の ConnectionError には、
    要求を送り終えたあとの切断(Connection aborted・RemoteDisconnected)も入るので、型だけでは決めない(破壊レポート1 弱点8)"""
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return True
    if not isinstance(e, requests.ConnectionError) or isinstance(e, requests.exceptions.ReadTimeout):
        return False
    seen, stack = set(), [e]
    while stack:  # 例外の中身(MaxRetryError.reason・args・__cause__・__context__)をたどる
        x = stack.pop()
        if x is None or id(x) in seen:
            continue
        seen.add(id(x))
        name = type(x).__name__
        if name in ("ProtocolError", "RemoteDisconnected", "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError", "ReadTimeoutError"):
            return False  # 送ったあとに切れた側の印が1つでもあれば、届いたかもしれない
        if name in ("NewConnectionError", "ConnectTimeoutError", "NameResolutionError"):
            return True
        stack += [getattr(x, "reason", None), getattr(x, "__cause__", None), getattr(x, "__context__", None)]
        stack += [a for a in getattr(x, "args", ()) if isinstance(a, BaseException)]
    return False


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
        r = http.post(base + "/poll.php", headers={"X-License-Key": key},
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
            http.post(base + "/result.php", headers={"X-License-Key": key}, json={"install_id": iid, **res}, timeout=POLL_TIMEOUT, allow_redirects=False)
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
