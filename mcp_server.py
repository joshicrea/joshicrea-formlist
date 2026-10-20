"""フォームリスト作成ツールを MCP で使う（Claude Code・Codex 共通）。実行環境: Windows / Python 3.9+（mcp パッケージが要る）。
中身は app.py（127.0.0.1:8780）を HTTP で呼ぶだけ。確認ゲート・NGリスト・二重送信防止・送信中の保存拒否は app.py 側にあり、
この MCP はそれを呼ぶだけで、足したり外したりしない。本番送信を最後に通すのは、MCPクライアントの承認と、本人が全文を見て「送って」と言うこと。
この MCP の呼び出しだけで、人が全文を読んだかは確かめられない。app.py が動いていなければ、画面を開かずに自動で起動する。
起動: py mcp_server.py （stdio。通常は Claude Code / Codex が起動する）"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

HERE = Path(__file__).resolve().parent
MAIN_PORT = 8780
POOL = [8780, 8781, 8782]  # 同時に立てる画面サーバーの上限。1つが最大3本のブラウザを動かすので、増やすとPCが重くなる
TOKENS = {}  # ポートごとの、画面から取ったトークン
_ctx = threading.local()  # このスレッドが今どのサーバーを呼んでいるか。指定が無ければ画面で使う8780


def _port():
    return getattr(_ctx, "port", MAIN_PORT)


def _base():
    return f"http://127.0.0.1:{_port()}"
mcp = FastMCP("formlist")
Count = Annotated[int, Field(ge=1, le=200)]  # 0 を渡すとサーバー側で既定の20社に化けるので、最初から断る


def _pythonw():
    exe = sys.executable
    return exe[:-len("python.exe")] + "pythonw.exe" if exe.lower().endswith("python.exe") else exe


def _raw(method, path, body=None):
    req = urllib.request.Request(_base() + path, method=method, data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 headers={"X-Token": TOKENS.get(_port()) or "", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _token():
    with urllib.request.urlopen(_base() + "/", timeout=5) as r:
        TOKENS[_port()] = re.search(r"const TOKEN='([^']+)'", r.read().decode("utf-8")).group(1)


def _start():
    """画面を開かずに app.py を起動する。起動できなかったときの原文は 出力/app_起動ログ.txt に残す"""
    (HERE / "出力").mkdir(exist_ok=True)
    log = open(HERE / "出力" / "app_起動ログ.txt", "ab")
    subprocess.Popen([_pythonw(), "app.py"], cwd=HERE, env=dict(os.environ, FORMLIST_NO_BROWSER="1", FORMLIST_PORT=str(_port())), stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(30):
        time.sleep(0.5)
        try:
            _token()
            return
        except Exception:
            pass
    tail = (HERE / "出力" / "app_起動ログ.txt").read_text(encoding="utf-8", errors="replace")[-300:]
    raise RuntimeError("フォームリスト作成ツール(app.py)を起動できませんでした。起動ログの末尾: " + tail)


def _ensure():
    """app.py を動かし、画面から埋め込まれるトークンを取る。古い版が動いていたら、処理中でなければ入れ替える"""
    try:
        _token()
    except Exception:
        _start()
    code, text = _raw("GET", "/api/job")
    j = json.loads(text)
    mine = hashlib.sha256(b"".join((HERE / f).read_bytes() for f in ("app.py", "autosend.py", "ng_list.py", "site_checker.py"))).hexdigest()[:12]
    if j.get("here") not in (None, str(HERE)):
        raise RuntimeError("別のフォルダのフォームリスト作成ツールが動いています。そちらを終了してから呼び直してください: " + str(j.get("here")))
    if j.get("version") != mine:
        code, _ = _raw("POST", "/api/quit", {"if_idle": True})  # 何も動いていないときだけ、サーバー側が受ける
        if code != 200:
            raise RuntimeError("古い版の app.py が処理を実行中です。終わってから呼び直してください")
        time.sleep(2)
        _start()


def api(method, path, body=None):
    _ensure()
    code, text = _raw(method, path, body)
    try:
        data = json.loads(text)
    except Exception:
        data = {"error": text[:200]}
    if code == 403 and isinstance(data, dict) and data.get("code") == "token":  # 画面を起動し直した直後
        TOKENS[_port()] = None
        _ensure()
        code, text = _raw(method, path, body)
        data = json.loads(text)
    return code, data


def _wait(max_s):
    """動いている処理が終わるまで待つ(最大 max_s 秒)。終われば最後の状態を返す"""
    end = time.time() + max_s
    while True:
        _, j = api("GET", "/api/job")
        if not j.get("running") or time.time() >= end:
            return j
        time.sleep(2)


def _brief(j, n=25):
    lines = [l for l in j.get("lines", []) if l.strip()]
    return {"running": j.get("running"), "name": j.get("name"), "exit_code": j.get("code"), "last_lines": lines[-n:]}


def _verdict(r):
    v, sup = r.get("フォーム判定", ""), "補完" in (r.get("取得元") or "")
    if v == "フォームあり" and not sup:
        import autosend  # 送れるかの判定は自動送信と同じ関数(別の基準を持たない)
        weak = autosend._hp_verify_skip_reason(r)
        if weak:
            return ("確かめてから送れる", "ホームページがその会社のものか、電話・住所で確かめられていない: " + weak)
    if v == "フォームあり":
        return ("確かめてから送れる", "ホームページを検索で見つけた会社。照合の結果: " + (r.get("HPの照合") or "なし") + "。別の会社の可能性がある。ユーザーが確かめたと言った場合だけ supp=true にする") if sup else ("送れる", "問い合わせフォームあり")
    return {"要目視": ("手で確かめる", "このツールでは送らない。ホームページを開いてフォームがあれば手で送る"),
            "メールのみ": ("送れない", "メールアドレスのみ。フォームなし"), "見つからず": ("送れない", "ホームページが見つからない"),
            "robots禁止": ("送れない", "相手が自動で読むことを断っている"), "取得失敗": ("送れない", "読み込めなかった")}.get(v, (v or "未判定", ""))


@mcp.tool()
def formlist_status() -> dict:
    """いま動いている処理（探す・確認・練習・本番送信）と、最後の経過・終了コード、作成済みのリスト一覧、ツールの版（別のパソコンと同じ版か照合する印）を返す。"""
    _, j = api("GET", "/api/job")
    others = []
    for port in POOL[1:]:
        if _alive(port):
            _, oj = _on(port, api, "GET", "/api/job")
            others.append({"server": port, **_brief(oj, 8)})
    return {**_brief(j), "version": j.get("version"), "files": j.get("files", [])[:10], "other_servers": others}


def _on(port, fn, *a, **kw):
    """fn をポート port のサーバーに対して呼ぶ(このスレッドの間だけ)"""
    _ctx.port = port
    try:
        return fn(*a, **kw)
    finally:
        _ctx.port = MAIN_PORT


def _alive(port):
    """起動していないサーバーを新しく立てずに、動いているかだけを見る"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2):
            return True
    except Exception:
        return False


def _running_ports():
    out = []
    for port in POOL:
        if _alive(port):
            _, j = _on(port, api, "GET", "/api/job")
            if j.get("running"):
                out.append(port)
    return out


def _start_search_on_free_server(body):
    """空いているサーバーを探して、検索を1つ始める。戻りは (ポート, 結果)。全部ふさがっていれば (None, None)。
    入力の誤り(400)は、どのサーバーでも同じなので、その場で返す"""
    busy = len(_running_ports())
    body = {**body, "parallel": 3 if busy == 0 else 2}  # 同時に動く検索が増えたら、1つあたりの本数を減らして、PC全体のブラウザを増やしすぎない
    for port in POOL:
        code, d = _on(port, api, "POST", "/api/search", body)
        if code != 200:
            return port, {**d, "started": False}
        if d.get("started"):
            return port, d
    return None, None


@mcp.tool()
def formlist_search(area: str, industries: list[str] | None = None, keywords: str = "", sources: list[str] | None = None,
                    limit: int = 50, include_large: bool = False, wait_seconds: int = 90) -> dict:
    """会社を探してフォームリストを作る。area は「東京都江東区」のように市区町村まで。industries は
    ホテル・旅館／百貨店／商業施設／ブライダル／観光施設／住宅・不動産／結婚相談所／自動車・保険／携帯ショップ／美容・コスメ／アパレル／
    ライフスタイル雑貨／食品・生産者直売／教育機関／福祉施設／自治体・地域団体／メーカー／企業の社内イベント から選ぶ。
    戦略書の優先順位は 商業施設 → 住宅・不動産 → ホテル・旅館 → ブライダル → 百貨店。sources は google（既定）/osm。Yahoo!マップは外した。
    limit は調べる会社数の上限（実測の一例: 10社で約40秒）。大手・チェーンと推定した会社は既定で外す（include_large=true で含める）。一人サロン・小さな会社を探すなら industries に「サロン」を使い、include_large は false のまま。wait_seconds 秒まで待ち、終わらなければ formlist_status で続きを見る。"""
    body = {"area": area, "limit": limit, "industries": industries or [], "keywords": keywords, "sources": sources or ["google"], "include_large": include_large}
    port, d = _start_search_on_free_server(body)
    if port is None:
        return {"started": False, "error": f"同時に動かせる探す処理は{len(POOL)}つまでで、すべて動いています。formlist_status で確認して、終わってから呼び直してください"}
    if not d.get("started"):
        return {"started": False, "error": d.get("error") or "探す処理を始められませんでした"}
    return {"started": True, "server": port, **_on(port, lambda: _brief(_wait(wait_seconds)))}


@mcp.tool()
def formlist_search_many(area: str, searches: list[dict], limit: int = 50, include_large: bool = False, sources: list[str] | None = None,
                         wait_seconds: int = 90) -> dict:
    """複数の探す処理を、別々のサーバーで同時に走らせる(最大3つ)。searches は {"industries": [...], "keywords": "語 語"} の並び。
    たとえば「江東区でサロンと、ホテルと、フラワー教室を別々のリストで同時に」なら、3つ入れる。
    1つの探す処理の中でキーワードをいくつか入れる(formlist_search の keywords)と、1つのリストにまとまり、最大3本ずつ並行して取る。
    こちらは、リストを分けたいときに使う。区は同じ area で、業種やキーワードを変える。
    同時に動く数は、PCが重くならないよう最大3つ。それを超える分は、始めずに理由を返す。戻りはそれぞれの経過と、できたリストの名前。"""
    if not searches:
        return {"error": "searches が空です"}
    started, results = [], []
    for i, sr in enumerate(searches):
        body = {"area": area, "limit": limit, "industries": sr.get("industries") or [], "keywords": sr.get("keywords", ""),
                "sources": sources or ["google"], "include_large": include_large}
        port, d = _start_search_on_free_server(body)
        if port is None:
            results.append({"index": i, "started": False, "error": f"同時に動かせるのは{len(POOL)}つまでで、すべて動いています。この検索は始めていません"})
        elif not d.get("started"):
            results.append({"index": i, "started": False, "error": d.get("error") or "始められませんでした"})
        else:
            started.append((i, port))
    def waiter(item):
        i, port = item
        return {"index": i, "started": True, "server": port, **_on(port, lambda: _brief(_wait(wait_seconds)))}
    if started:
        with ThreadPoolExecutor(max_workers=len(started)) as ex:
            results += list(ex.map(waiter, started))
    return {"searches": sorted(results, key=lambda r: r["index"]), "note": "終わっていないものは formlist_status で続きを見る。できたリストは formlist_results で読む"}


@mcp.tool()
def formlist_results(file: str = "", only_sendable: bool = True) -> dict:
    """作成済みリストの中身を、会社ごとの判定（送れる／確かめてから送れる／手で確かめる／送れない）と理由つきで返す。
    file を省くと最新のリスト。営業お断りの表示がある会社・送らないリストの会社は最初から含まれない。"""
    _, j = api("GET", "/api/job")
    files = j.get("files", [])
    f = file or (files[0] if files else "")
    if not f:
        return {"error": "まだリストがありません。formlist_search で作ってください"}
    code, rows = api("GET", "/api/results?file=" + urllib.request.quote(f))
    if code != 200:
        return rows
    out, cnt = [], {}
    for r in rows:
        k, why = _verdict(r)
        cnt[k] = cnt.get(k, 0) + 1
        if only_sendable and k in ("送れない",):
            continue
        out.append({"company": r.get("企業名"), "industry": r.get("業種"), "verdict": k, "why": why, "hp": r.get("HP URL"),
                    "form": r.get("問い合わせフォームURL 無ければメール"), "source": r.get("取得元")})
    out.sort(key=lambda x: ["送れる", "確かめてから送れる", "手で確かめる"].index(x["verdict"]) if x["verdict"] in ("送れる", "確かめてから送れる", "手で確かめる") else 3)
    return {"file": f, "counts": cnt, "total": len(rows), "companies": out}


@mcp.tool()
def formlist_ng_add(hp: str = "", form: str = "", name: str = "") -> dict:
    """この会社は送らない、とNGリストに入れる。hp（ホームページURL）のドメインごと入る。同じホームページの別施設も以後出なくなる。
    共有サービス上のページは、そのURLだけが入る。ユーザーが送らないと言った会社だけに使う。入れたら送る会社が変わるので、formlist_preview をやり直す。"""
    code, d = api("POST", "/api/ng", {"hp": hp, "form": form, "name": name})
    return {**d, "note": "送らない会社が変わったので、本番の前に formlist_preview をやり直す"} if code == 200 else {"error": d.get("error")}


@mcp.tool()
def formlist_ng_list() -> dict:
    """画面・MCPの「送らない」で入れた行の一覧（formlist_ng_undo で戻せる対象）。"""
    _, d = api("GET", "/api/ngscreen")
    return {"lines": d}


@mcp.tool()
def formlist_ng_undo(line: str) -> dict:
    """ユーザーが戻すと明示した行だけに使う。formlist_ng_list にある行を、送らないリストから戻す。自動で入った行（営業お断り）は戻せない。
    戻すと送る会社が変わるので、formlist_preview をやり直す。"""
    code, d = api("POST", "/api/ng/undo", {"line": line})
    return d if code == 200 else {"error": d.get("error")}


@mcp.tool()
def formlist_sender_get() -> dict:
    """保存されている送信元（会社名・氏名・メール・電話・住所・署名など）と、足りない必須項目。"""
    _, m = api("GET", "/api/meta")
    s = m.get("sender", {})
    miss = [k for k in ("会社名", "氏名", "メール", "署名") if not (s.get(k) or "").strip() or "【" in s.get(k, "")]
    return {"sender": s, "missing_required": miss}


@mcp.tool()
def formlist_sender_set(fields: dict) -> dict:
    """送信元の項目を更新する（渡した項目だけ変わる）。使える項目: 会社名 氏名 フリガナ メール 電話 郵便番号 住所 件名 署名 資料URL（文面の {資料URL} に入る。https:// で始まるURL。費用を載せた資料は置かない）。
    署名は顧客に届く。変更内容の全文をユーザーに見せ、承認を得てから保存する。署名には連絡先と配信停止の案内を含める。
    保存すると確認済みの印は無効になり、formlist_preview からやり直しになる。それ以外の項目名は断る。"""
    code, d = api("POST", "/api/sender", fields)
    return d if code == 200 else {"error": d.get("error")}


@mcp.tool()
def formlist_message_get(name: str = "default") -> dict:
    """送る文面を読む。name は default（標準）か業種名。{企業名}{書き出し}{署名} は送るとき自動で置き換わる。"""
    _, m = api("GET", "/api/meta")
    msgs = m.get("messages", {})
    return {"name": name, "text": msgs.get(name, msgs.get("default", "")), "available": list(msgs)}


@mcp.tool()
def formlist_message_set(name: str, text: str) -> dict:
    """送る文面を保存する。顧客に届く文面なので、ユーザーに全文を見せて承認を得てから保存する。保存すると確認済みの印は無効になる。"""
    code, d = api("POST", "/api/message", {name: text})
    return d if code == 200 else {"error": d.get("error")}


@mcp.tool()
def formlist_message_sample(material: str) -> dict:
    """営業資料の文章から、営業文のたたき台を作る(保存はしない)。見出し・箇条書き・URLを拾う型どおりの文なので、AIが資料を読んで書き直し、ユーザーに全文を見せて承認を得てから formlist_message_set で保存する。"""
    code, d = api("POST", "/api/sample", {"material": material})
    return d if code == 200 else {"error": d.get("error")}


@mcp.tool()
def formlist_openings_missing(file: str) -> dict:
    """書き出しが空の会社の一覧(会社名・業種・ホームページ)。文面に {書き出し} がある場合、空の会社は送信の対象から外れる。
    各社のホームページを読み、その会社に合う1〜2文の書き出しを書いて formlist_openings_set で保存する。顧客に届く文なので、全文をユーザーに見せて承認を得てから保存する。"""
    code, d = api("GET", f"/api/openings?file={urllib.request.quote(file)}")
    return d if code == 200 else {"error": d.get("error")}


@mcp.tool()
def formlist_openings_set(file: str, openings: dict[str, str]) -> dict:
    """会社ごとの書き出しをリストのCSVに保存する(会社名 → 書き出し)。保存すると確認済みの印は無効になる。ユーザーに全文を見せて承認を得てから呼ぶ。"""
    code, d = api("POST", "/api/openings", {"file": file, "openings": openings})
    return d if code == 200 else {"error": d.get("error")}


@mcp.tool()
def formlist_practice(file: str, max_companies: Count = 3, supp: bool = False, wait_seconds: int = 120) -> dict:
    """練習: 実際のフォームに入力して画面を保存するだけ。送信ボタンは押さない。"""
    code, d = api("POST", "/api/send", {"file": file, "max": max_companies, "supp": supp, "send": False})
    if code != 200 or not d.get("started"):
        return {"started": False, "error": d.get("error") or "ほかの処理が動いています"}
    return {"started": True, **_brief(_wait(wait_seconds), 40)}


@mcp.tool()
def formlist_preview(file: str, max_companies: Count = 3, supp: bool = False) -> dict:
    """本番の前の確認。送る対象の会社と、各社に送る文面の全文を返す（送らない）。本番を通すための「確認済みの印」を立てる操作でもある。
    本番は、送る会社と各社の全文がこの確認と同じときだけ通る。送らないリスト・送信済み・文面・送信元が変われば、やり直しになる。
    返った全文は、そのままユーザーに見せる。人が読んだかどうかは、この呼び出しでは確かめられない。"""
    code, d = api("POST", "/api/send", {"file": file, "max": max_companies, "supp": supp, "send": False, "preview": True})
    if code != 200 or not d.get("started"):
        return {"started": False, "error": d.get("error") or "ほかの処理が動いています"}
    j = _wait(120)
    if j.get("running") or j.get("code") != 0:
        return {"ok": False, **_brief(j, 40)}
    code, t = api("GET", f"/api/targets?file={urllib.request.quote(file)}&max={max_companies}&supp={1 if supp else 0}")
    if code != 200:
        return {"ok": False, "error": t.get("error")}
    _, m = api("GET", "/api/meta")
    s = m.get("sender", {})
    return {"ok": True, "count": len(t), "sender": f"{s.get('会社名', '')} {s.get('氏名', '')} {s.get('メール', '')}",
            "companies": [{"company": x["name"], "industry": x["industry"], "form": x["form"], "message": x["text"]} for x in t],
            "next": "全文をユーザーに見せ、ツールの画面で「この全文で送ることを承認する」を押してもらう。承認後に「送って」と言われたら formlist_send を同じ引数で呼ぶ。見せたあとに送らない会社などを変えたら、preview からやり直す"}


@mcp.tool()
def formlist_send(file: str, max_companies: Count, supp: bool, confirm: str) -> dict:
    """本番送信。顧客に実際に届く。formlist_preview の全文をユーザーに見せ、ユーザーが「送って」と明示した後にだけ呼ぶ。
    file・max_companies・supp は直前の formlist_preview と同じ値にする。confirm には「送信」と入れる。
    人の承認が要る: ユーザーがツールの画面で全文を読み、「この全文で送ることを承認する」を押すまで、送れない(AIは承認を押せない。承認は1回の送信で使い切る)。
    送信中は文面と送信元を保存できない。"""
    code, d = api("POST", "/api/send", {"file": file, "max": max_companies, "supp": supp, "send": True, "confirm": confirm})
    if code != 200 or not d.get("started"):
        return {"started": False, "error": d.get("error") or "ほかの処理が動いています"}
    return {"started": True, "note": "送信は1社ごとに間隔を空けて進む。formlist_status で経過、formlist_log で結果を見る", **_brief(_wait(20))}


@mcp.tool()
def formlist_log(limit: int = 30) -> dict:
    """送信の記録（練習・本番）。結果の意味: 練習OK=送っていない／送信済み／手で送る必要あり=フォームを開いて手で送る／
    送れたか確認が要る=送信ボタンは押した。相手の自動返信メールで確かめる。"""
    _, rows = api("GET", "/api/log")
    return {"total": len(rows), "recent": rows[-limit:][::-1]}


if __name__ == "__main__":
    mcp.run()
