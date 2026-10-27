"""フォームリスト作成ツールを MCP で使う（Claude Code・Codex 共通）。実行環境: Windows / Python 3.9+（mcp パッケージが要る）。
中身は app.py（127.0.0.1:8780）を HTTP で呼ぶだけ。確認ゲート・NGリスト・二重送信防止・送信中の保存拒否は app.py 側にあり、
この MCP はそれを呼ぶだけで、足したり外したりしない。本番送信を最後に通すのは、MCPクライアントの承認と、本人が全文を見て「送って」と言うこと。
この MCP の呼び出しだけで、人が全文を読んだかは確かめられない。app.py が動いていなければ、画面を開かずに自動で起動する。
起動: py mcp_server.py （stdio。通常は Claude Code / Codex が起動する）"""
import hashlib
import json
import os
import re
import socket
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
import ports
import updater
from ports import POOL  # 同時に立てる画面サーバーの番号と上限(定義は ports.py。1つが最大3本のブラウザを動かすので、増やすとPCが重くなる)
TOKENS = {}  # ポートごとの、画面から取ったトークン
_ctx = threading.local()  # このスレッドが今どのサーバーを呼んでいるか。指定が無ければ画面で使う8780


def _port():
    return getattr(_ctx, "port", None) or MAIN_PORT


def _base():
    return f"http://127.0.0.1:{_port()}"
# 127.0.0.1 宛ての通信を、会社のプロキシに回さない(OSのプロキシ設定が「ローカルを除く」になっていなくても、つながるように)
_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_MAIN_RESOLVED = False
mcp = FastMCP("formlist", instructions="""AIまるなげ営業（営業部）。接続したら最初に formlist_guide を呼び、その手順で利用者を案内する。
会社探し→ホームページの照合→文面→全文の確認→本人の承認（画面の外の確認ダイアログ）→送信、の順を守る。
本人が全文を見て「送って」と明示するまで、formlist_send の本番（send=true）を呼ばない。承認を代わりに押さない。""")
_LOADED = hashlib.sha256((HERE / "mcp_server.py").read_bytes()).hexdigest()  # 起動したときの版。ディスクの版と違えば、古いMCPが動いている
Count = Annotated[int, Field(ge=1, le=300)]  # 0 を渡すとサーバー側で既定の20社に化けるので、最初から断る


def _pythonw():
    exe = sys.executable
    return exe[:-len("python.exe")] + "pythonw.exe" if exe.lower().endswith("python.exe") else exe


def _raw(method, path, body=None):
    req = urllib.request.Request(_base() + path, method=method, data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 headers={"X-Token": TOKENS.get(_port()) or "", "Content-Type": "application/json"})
    try:
        with _OPEN.open(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _token():
    with _OPEN.open(_base() + "/", timeout=5) as r:
        m = re.search(r"const TOKEN='([^']+)'", r.read().decode("utf-8"))
    if not m:
        raise RuntimeError(f"{_port()}番は、このツールではない別のアプリが使っています")
    TOKENS[_port()] = m.group(1)


def _start():
    """画面を開かずに app.py を起動する。起動できなかったときの原文は 出力/app_起動ログ.txt に残す"""
    (HERE / "出力").mkdir(exist_ok=True)
    with open(HERE / "出力" / "app_起動ログ.txt", "ab") as log:
        subprocess.Popen([_pythonw(), "-X", "utf8", "app.py"], cwd=HERE,
                         env=dict(os.environ, FORMLIST_NO_BROWSER="1", FORMLIST_PORT=str(_port()), PYTHONIOENCODING="utf-8"),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(30):
        time.sleep(0.5)
        try:
            _token()
            return
        except Exception:
            pass
    tail = (HERE / "出力" / "app_起動ログ.txt").read_text(encoding="utf-8", errors="replace")[-300:]
    raise RuntimeError("フォームリスト作成ツール(app.py)を起動できませんでした。起動ログの末尾: " + tail)


def _can_bind(port):
    """そのポートが本当に空いているか。他のアプリが使っている・Windowsが予約している(Hyper-V/WSL/Docker)ときは False"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _ours(port):
    """そのポートで動いているのが、このフォルダのツールか"""
    j = ports.job(port)  # トークンが要らない /api/whoami で確かめる(/api/job で確かめると、動いている自分のツールを別のアプリと取り違える)
    return j is not None and j.get("here") in (None, str(HERE))


def _resolve_main():
    """状態確認・確認・送信の窓口にするポートを決める。動いている自分のサーバーがあればそれ、無ければ空いているポート。
    他のアプリが8780を使っていても、次の番号に進む"""
    global MAIN_PORT, _MAIN_RESOLVED
    for p in POOL:
        if _ours(p):
            MAIN_PORT, _MAIN_RESOLVED = p, True
            return
    for p in POOL:
        if _can_bind(p):
            MAIN_PORT, _MAIN_RESOLVED = p, True
            return
    raise RuntimeError(f"ポート{POOL[0]}〜{POOL[-1]}が、すべてほかのアプリに使われているか、Windowsに予約されています。使っているアプリを閉じるか、パソコンを再起動してから、もう一度お試しください")


def _ensure():
    """app.py を動かし、画面から埋め込まれるトークンを取る。古い版が動いていたら、処理中でなければ入れ替える"""
    if getattr(_ctx, "port", None) is None and not _MAIN_RESOLVED:
        _resolve_main()
    try:
        _token()
    except Exception:
        _start()
    code, text = _raw("GET", "/api/job")
    try:
        j = json.loads(text)
    except ValueError:
        raise RuntimeError(f"{_port()}番から、このツールの応答が返りませんでした（別のアプリが使っている可能性があります）: {text[:120]}")
    if j.get("here") not in (None, str(HERE)):
        raise RuntimeError("別のフォルダのフォームリスト作成ツールが動いています。そちらを終了してから呼び直してください: " + str(j.get("here")))
    # 画面のサーバーが読み込んだ版(build)が、ディスクの版と違えば、古いまま動いている。build を知らない古い画面なら、従来の4ファイルの印で見る
    if "build" in j:
        stale = j["build"] != updater.build()
    else:
        stale = j.get("version") != hashlib.sha256(b"".join((HERE / f).read_bytes() for f in ("app.py", "autosend.py", "ng_list.py", "site_checker.py"))).hexdigest()[:12]
    if stale:
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


_OUTCOME_TEXT = {  # 内訳に message が無い古い印のときだけ使う
    "ok": "会社が見つかりました。",
    "zero": "失敗ではなく、条件に合う会社が見つかりませんでした（0件）。地名の書き方・業種・キーワードを変えると見つかることがあります。",
    "failed": "取得に失敗しました。これは「0件」ではありません。通信の失敗か、Googleに止められた可能性があります。失敗の原文を利用者に伝え、時間をおいて探し直してください。",
    "partial": "一部の検索が失敗しましたが、取れた分で続けました。取りこぼしがあります（件数は summary を見る）。",
}


def _run_summary(port=None, job_started=""):
    """直近の探す処理の内訳。サーバー(ポート)ごとの印を読む。無い・読めない・今回の処理より古いときは None"""
    p = HERE / "出力" / f"最後の探索_内訳_{port or _port()}.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if job_started and (d.get("started") or "") < job_started:  # 前の回の印。今回の結果として使わない
        return None
    d["plain"] = d.get("message") or _OUTCOME_TEXT.get(d.get("outcome"), "")
    if d.get("outcome") == "running":  # 探す処理は終わっているのに、印が「途中」のまま = 異常終了か、止められた
        d["plain"] = "探す処理が途中で止まりました（異常終了したか、止められました）。最後の経過(last_lines)の原文を利用者に伝えてください。リストは作られていないことがあります。"
    return d


def _brief(j, n=25):
    lines = [l for l in j.get("lines", []) if l.strip()]
    out = {"running": j.get("running"), "name": j.get("name"), "exit_code": j.get("code"), "last_lines": lines[-n:]}
    if not j.get("running") and j.get("name") == "探す":
        sm = _run_summary(job_started=j.get("started") or "")
        if sm:
            out["summary"] = sm
    return out


def _verdict(r):
    v, sup = r.get("フォーム判定", ""), "補完" in (r.get("取得元") or "")
    if v == "フォームあり" and not sup:
        import autosend  # 送れるかの判定は自動送信と同じ関数(別の基準を持たない)
        weak = autosend._hp_verify_skip_reason(r)
        if weak:
            return ("確かめてから送れる", "ホームページがその会社のものか、電話・住所で確かめられていない: " + weak)
    if v == "フォームあり":
        return ("確かめてから送れる", "ホームページを検索で見つけた会社。照合の結果: " + (r.get("HPの照合") or "なし") + "。別の会社の可能性がある。ユーザーが確かめたと言った場合だけ supp=true にする") if sup else ("送れる", "問い合わせフォームあり")
    if v == "見つからず" and "SNS・予約サイト・掲載サイト" in (r.get("判定メモ") or ""):
        return ("送れない", "地図に載っていたのは予約・掲載・SNSのページで、会社のホームページは分かっていない。公式HPを探し直すか、電話で当たる")
    return {"要目視": ("手で確かめる", "このツールでは送らない。ホームページを開いてフォームがあれば手で送る"),
            "メールのみ": ("送れない", "メールアドレスのみ。フォームなし"), "見つからず": ("送れない", "ホームページが見つからない"),
            "robots禁止": ("送れない", "相手が自動で読むことを断っている"), "取得失敗": ("送れない", "読み込めなかった")}.get(v, (v or "未判定", ""))


_UPDATE_CACHE = {}  # {"at": 取った時刻, "result": 照合の結果}。1時間は取り直さない


def _update_check():
    """手元の本体ファイルが公開リポジトリの最新(manifest.json)と同じか。取れなかったときは「未確認」で、「最新」とは言わない"""
    if _UPDATE_CACHE and time.time() - _UPDATE_CACHE["at"] < 3600:
        return _UPDATE_CACHE["result"]
    c = updater.check()
    r = {"state": c["state"], "stale_files": c["files"], "message": c["message"]}
    if c["state"] == "古い":
        r["message"] = "新しい版があります。ツールの画面を開くと、自動で更新されます（formlist_update でも更新できます）。送信元・文面・送らないリスト・できたリストは残ります"
    _UPDATE_CACHE.update(at=time.time(), result=r)
    return r


@mcp.tool()
def formlist_update() -> dict:
    """公開されている最新の版へ更新する(探す・送る処理が動いていないときだけ)。画面のサーバーは自動で起動し直される。
    mcp_server.py が更新に含まれていたときは、Claude を完全に終了して(タスクトレイも)開き直すまで、新しい道具は使えない。結果の need_restart_claude を、そのまま利用者に伝える。"""
    code, d = api("POST", "/api/update", {})
    if code != 200 or not isinstance(d, dict):
        return {"updated": [], "error": (d or {}).get("error", f"更新できませんでした（応答 {code}）")}
    _UPDATE_CACHE.clear()
    if d.get("error"):
        return {"updated": d.get("updated", []), "error": d["error"]}
    out = {"updated": d.get("updated", []), "restart_screen": bool(d.get("restart"))}
    if "mcp_server.py" in out["updated"]:
        out["need_restart_claude"] = "更新に、Claude の道具の本体(mcp_server.py)が含まれています。Claude を完全に終了して(画面右下の「^」の中の Claude も)開き直すまで、新しい道具は使えません"
    if not out["updated"]:
        out["message"] = "すでに最新です"
    return out


@mcp.tool()
def formlist_guide() -> dict:
    """営業部の手引き。接続したら最初に呼ぶ。役割分担・進める順番・してはいけないこと・失敗したときの伝え方が入っている。この手順で利用者を案内する。"""
    p = HERE / "営業部_手引き.md"
    try:
        return {"guide": p.read_text(encoding="utf-8")}
    except OSError as e:
        return {"error": f"手引きを読めませんでした: {e}。ツールを入れ直すと直ります（install.ps1 をもう一度実行）"}


@mcp.tool()
def formlist_status() -> dict:
    """いま動いている処理（探す・確認・練習・本番送信）と、最後の経過・終了コード、作成済みのリスト一覧、ツールの版（別のパソコンと同じ版か照合する印）を返す。
    update は公開されている最新の版との照合(最新/古い/未確認)。古いときは message をそのまま利用者に伝える。未確認を「最新」と言い換えない。"""
    _, j = api("GET", "/api/job")
    others = []
    for port in POOL[1:]:
        if _alive(port):
            _, oj = _on(port, api, "GET", "/api/job")
            others.append({"server": port, **_on(port, _brief, oj, 8)})
    out = {**_brief(j), "version": j.get("version"), "screen": f"http://127.0.0.1:{_port()}/", "files": j.get("files", [])[:10], "other_servers": others,
           "update": _update_check()}
    if hashlib.sha256((HERE / "mcp_server.py").read_bytes()).hexdigest() != _LOADED:  # 更新後もClaudeが古いMCPを動かし続けている
        out["warning"] = "このMCPは更新前の古い版のまま動いています。利用者にClaudeを完全に終了して(タスクトレイも)開き直してもらってください。開き直すまで、新しい手引きと道具が使えません"
    _, unsure = api("GET", "/api/unsure")
    if isinstance(unsure, list) and unsure:
        out["need_check"] = f"届いたか確かめが要る会社が{len(unsure)}社あります。formlist_unsure で一覧を出し、利用者に確かめてもらって formlist_outcome_set で記録する"
    return out


def _on(port, fn, *a, **kw):
    """fn をポート port のサーバーに対して呼ぶ(このスレッドの間だけ)"""
    _ctx.port = port
    try:
        return fn(*a, **kw)
    finally:
        _ctx.port = None


def _alive(port):
    """起動していないサーバーを新しく立てずに、動いているかだけを見る"""
    return _ours(port)


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
        try:
            code, d = _on(port, api, "POST", "/api/search", body)
        except RuntimeError:  # 他のアプリが使っている・Windowsが予約していて起動できない番号は飛ばす
            continue
        if code != 200:
            return port, {**d, "started": False}
        if d.get("started"):
            return port, d
    return None, None


@mcp.tool()
def formlist_search(area: str, industries: list[str] | None = None, keywords: str = "", sources: list[str] | None = None,
                    limit: int = 50, include_large: bool = True, wait_seconds: int = 90) -> dict:
    """会社を探してフォームリストを作る。area は「東京都江東区」のように市区町村まで。industries は
    ホテル・旅館／百貨店／商業施設／ブライダル／観光施設／住宅・不動産／結婚相談所／自動車・保険／携帯ショップ／美容・コスメ／アパレル／
    ライフスタイル雑貨／食品・生産者直売／教育機関／福祉施設／自治体・地域団体／メーカー／企業の社内イベント／サロン から選ぶ。
    戦略書の優先順位は 商業施設 → 住宅・不動産 → ホテル・旅館 → ブライダル → 百貨店。sources は google（既定）/osm。Yahoo!マップは外した。
    一人サロン・小さな会社なら「サロン」。一覧に無い業種は industries に入れず、keywords に語として入れる。
    limit は調べる会社数の上限。かかる時間の見込みは、始めた直後の経過(last_lines)と、終わったあとの summary.estimate_minutes に出る。利用者には見込みを伝え、やめたいと言われたら formlist_stop を呼ぶ。
    大手・チェーンと推定した会社は既定で含める（include_large=false で外す）。一人サロン・小さな会社だけにしたいときだけ include_large=false にする。wait_seconds 秒まで待ち、終わらなければ formlist_status で続きを見る。"""
    miss = _chromium_missing()
    if miss:
        return {"started": False, "error": miss}
    body = {"area": area, "limit": limit, "industries": industries or [], "keywords": keywords, "sources": sources or ["google"], "include_large": include_large,
            "official_research": True}
    port, d = _start_search_on_free_server(body)
    if port is None:
        return {"started": False, "error": f"同時に動かせる探す処理は{len(POOL)}つまでで、すべて動いています。formlist_status で確認して、終わってから呼び直してください"}
    if not d.get("started"):
        return {"started": False, "error": d.get("error") or "探す処理を始められませんでした"}
    return {"started": True, "server": port, **_on(port, lambda: _brief(_wait(wait_seconds)))}


@mcp.tool()
def formlist_search_many(area: str, searches: list[dict], limit: int = 50, include_large: bool = True, sources: list[str] | None = None,
                         wait_seconds: int = 90) -> dict:
    """複数の探す処理を、別々のサーバーで同時に走らせる(最大3つ)。searches は {"industries": [...], "keywords": "語 語"} の並び。
    たとえば「江東区でサロンと、ホテルと、フラワー教室を別々のリストで同時に」なら、3つ入れる。
    1つの探す処理の中でキーワードをいくつか入れる(formlist_search の keywords)と、1つのリストにまとまり、最大3本ずつ並行して取る。
    こちらは、リストを分けたいときに使う。区は同じ area で、業種やキーワードを変える。
    同時に動く数は、PCが重くならないよう最大3つ。それを超える分は、始めずに理由を返す。戻りはそれぞれの経過と、できたリストの名前。"""
    if not searches:
        return {"error": "searches が空です"}
    miss = _chromium_missing()
    if miss:
        return {"error": miss}
    started, results = [], []
    for i, sr in enumerate(searches):
        body = {"area": area, "limit": limit, "industries": sr.get("industries") or [], "keywords": sr.get("keywords", ""),
                "sources": sources or ["google"], "include_large": include_large, "official_research": True}
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
    out, other, cnt = [], [], {}
    routes = {"フォーム": 0, "電話": 0, "メール": 0, "LINE公式": 0}
    for r in rows:
        k, why = _verdict(r)
        cnt[k] = cnt.get(k, 0) + 1
        contact = {"phone": r.get("比較電話") or "", "address": r.get("比較住所") or "", "mail": r.get("メールアドレス") or "", "line": r.get("LINE公式") or ""}
        for name, v in (("フォーム", k in ("送れる", "確かめてから送れる")), ("電話", contact["phone"]), ("メール", contact["mail"]), ("LINE公式", contact["line"])):
            routes[name] += 1 if v else 0
        item = {"company": r.get("企業名"), "industry": r.get("業種"), "verdict": k, "why": why, "hp": r.get("HP URL"),
                "form": r.get("問い合わせフォームURL 無ければメール"), "source": r.get("取得元"), **{a: b for a, b in contact.items() if b}}
        if only_sendable and k in ("送れない",):
            if any(contact[x] for x in ("phone", "mail", "line")):
                other.append(item)
            continue
        out.append(item)
    out.sort(key=lambda x: ["送れる", "確かめてから送れる", "手で確かめる"].index(x["verdict"]) if x["verdict"] in ("送れる", "確かめてから送れる", "手で確かめる") else 3)
    res = {"file": f, "counts": cnt, "total": len(rows), "routes": routes, "companies": out, "other_routes": other,
           "note": "companies はフォームで送る候補。other_routes はフォームでは送れないが、電話・メール・LINE公式で当たれる会社(このツールからは送らない)。利用者には両方の件数を伝える"}
    for port in POOL:  # このリストを作った回の内訳(地域・始めた時刻・確かめていないこと)
        sm = _run_summary(port)
        if sm and sm.get("list_file") == f:
            res.update({k: sm.get(k) for k in ("area", "started", "outcome", "missed", "out_of_area", "not_checked", "area_notes")})
            break
    return res


def _chromium_missing():
    """探すのに要るブラウザ部品(Chromium)が入っているか。無ければ利用者向けの理由、あれば空。一度入っていると分かれば、以後は見ない"""
    global _CHROMIUM_OK
    if _CHROMIUM_OK:
        return ""
    code = "from playwright.sync_api import sync_playwright\nwith sync_playwright() as p: print(p.chromium.executable_path)"
    try:
        # stdin を DEVNULL にする: MCP サーバーの標準入力(閉じない管)を引き継ぐと、Playwright の起動が40秒以上かかり、60秒で時間切れになる
        # (実測 2026-10-26: 引き継ぎ 40.1秒 / DEVNULL 2.9秒。新しいパソコンで「Chromium が入っていません」と誤って出た)
        r = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                           stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        path = (r.stdout or "").strip().splitlines()[-1:] or [""]
        if r.returncode == 0 and Path(path[0]).exists():
            _CHROMIUM_OK = True
            return ""
        why = (r.stderr or r.stdout or "").strip()[-300:]
    except subprocess.TimeoutExpired:
        return ""  # 確認が終わらないだけでは「入っていない」と言い切れない。そのまま探しはじめて、本当に無ければ探す処理が原文で止まる
    except Exception as e:
        why = repr(e)
    return ("会社を探すのに使うブラウザの部品(Chromium)が入っていません。導入の1行(install.ps1)をもう一度実行すると入ります。"
            "それでも直らなければ、この原文をツールをお渡しした担当者に伝えてください: " + why)


_CHROMIUM_OK = False


@mcp.tool()
def formlist_stop() -> dict:
    """動いている「探す」処理を止める。調べ終わった分だけでリストを作って終わる(数十秒かかる)。送信(本番送信・確認・練習)は止めない。
    利用者が「やめて」「時間がかかりすぎ」と言ったときに使う。"""
    stopped = []
    for port in POOL:
        if not _alive(port):
            continue
        _, j = _on(port, api, "GET", "/api/job")
        if j.get("running") and j.get("name") == "探す":
            (HERE / "出力").mkdir(exist_ok=True)
            (HERE / "出力" / f"探す_止める_{port}.flag").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            stopped.append(port)
    if not stopped:
        return {"stopped": [], "note": "止める「探す」処理はありませんでした"}
    return {"stopped": stopped, "note": "止める指示を出しました。調べ終わった分でリストを作って終わります。formlist_status で終わったかを見て、summary の outcome が stopped になっていれば止まっています"}


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
    """送信元の項目を更新する（渡した項目だけ変わる）。使える項目: 会社名 氏名 フリガナ メール 電話 郵便番号 住所 件名 署名 資料URL（文面の {資料URL} に入る。https:// で始まるURL。費用を載せた資料は置かない）と 計測キー（資料URLの {識別} 用。担当者から届いた設定の文に書いてあるものを、そのまま渡す。保存して、画面にも応答にも出さない。先に渡してから資料URLを渡す）。
    署名は顧客に届く。変更内容の全文をユーザーに見せ、承認を得てから保存する。署名には連絡先と配信停止の案内を含める。
    保存すると確認済みの印は無効になり、formlist_preview からやり直しになる。それ以外の項目名は断る。"""
    code, d = api("POST", "/api/sender", fields)
    return d if code == 200 else {"error": d.get("error")}


@mcp.tool()
def formlist_message_get(name: str = "default") -> dict:
    """送る文面を読む。name は default（標準）か業種名（業種の一覧は formlist_status ではなく、返り値の by_industry）。{企業名}{書き出し}{署名} は送るとき自動で置き換わる。
    by_industry は、業種ごとに「専用」(その業種専用の文面がある)か「標準」(専用が無く、標準の文面が使われる)。orphans は、古い業種名で作った文面(いまは使われない)で、to が引き継ぎ先の候補。
    業種ごとに文面が違うので、最初の設定のときに、探す業種ぶんをまとめて決める(手引きの「文面を業種ごとに用意する」)。"""
    _, m = api("GET", "/api/meta")
    msgs = m.get("messages", {})
    info = m.get("message_info", {})
    return {"name": name, "text": msgs.get(name, msgs.get("default", "")), "uses": "専用" if name in msgs and name != "default" else "標準",
            "by_industry": info.get("industries", {}), "orphans": info.get("orphans", []), "available": list(msgs)}


@mcp.tool()
def formlist_message_set(name: str, text: str) -> dict:
    """送る文面を保存する。顧客に届く文面なので、ユーザーに全文を見せて承認を得てから保存する。保存すると確認済みの印は無効になる。"""
    code, d = api("POST", "/api/message", {name: text})
    return d if code == 200 else {"error": d.get("error")}


@mcp.tool()
def formlist_message_move(name: str, to: str) -> dict:
    """古い業種名で作った文面(formlist_message_get の orphans)を、いまの業種の専用の文面にコピーする(元は残る。すでに専用がある業種には上書きしない)。
    顧客に届く文面なので、コピーしたあとで formlist_message_get で全文を読み、利用者に見せて、必要なら直して formlist_message_set で保存する。"""
    code, d = api("POST", "/api/message/move", {"from": name, "to": to})
    return d if code == 200 else {"error": (d or {}).get("error", f"できませんでした（応答 {code}）")}


@mcp.tool()
def formlist_message_unset(name: str) -> dict:
    """業種の専用の文面をやめて、標準の文面を使うようにする(前の文面は 文面/_削除済み に残る)。利用者が頼んだときだけ使う。"""
    code, d = api("POST", "/api/message/delete", {"name": name})
    return d if code == 200 else {"error": (d or {}).get("error", f"できませんでした（応答 {code}）")}


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
def formlist_practice(file: str, max_companies: Annotated[int, Field(ge=1, le=3)] = 3, supp: bool = False, wait_seconds: int = 120, user_agreed: bool = False) -> dict:
    """練習: 実際のフォームに入力して画面を保存するだけ。送信ボタンは押さない。ただし、入力の途中で内容を先方に届く作りのフォームがある
    （氏名・電話・文面が先方に渡りうる）。必ず利用者にその旨を伝えて了解を得てから、user_agreed=true で呼ぶ。了解なしには動かさない。一度に3社まで。"""
    if not user_agreed:
        return {"started": False, "error": "練習は、実在の会社のフォームに入力します。入力の途中で内容が先方に届く作りのフォームもあります。利用者に伝えて了解を得てから、user_agreed=true で呼んでください"}
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
            "screen": f"http://127.0.0.1:{_port()}/",
            "next": ("1) この全文を利用者に見せる。2) 利用者に、ツールの画面(screen のアドレス)の「3 送る」を開いてもらい、同じ全文が出ていることを確かめてもらう。"
                     "3) 利用者が画面の本番のボタンを押すと、画面の外に確認ダイアログが出るので、本人が「はい」を押す。4) そのあと利用者が「送って」と言ったら formlist_send を同じ引数で呼ぶ。"
                     "AIは承認を押せない。見せたあとに送らない会社などを変えたら、preview からやり直す"),
            "legal_missing": t_legal(s)}


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


def t_legal(sender):
    import send_rules
    return send_rules.legal_missing(sender)


@mcp.tool()
def formlist_unsure() -> dict:
    """送信ボタンを押したかもしれないが、届いたか確かめていない会社の一覧。送ったあとに必ず呼ぶ。
    利用者に、相手からの自動返信メール(受付のお知らせ)か、送信後の画面の保存(出力/送信画面)で確かめてもらい、formlist_outcome_set で記録する。勝手に送り直さない。"""
    _, d = api("GET", "/api/unsure")
    return {"companies": d, "count": len(d) if isinstance(d, list) else 0}


@mcp.tool()
def formlist_outcome_set(company: str, form: str, delivered: bool, how: str = "") -> dict:
    """届いたかを確定させる。利用者が確かめた結果だけを入れる(推測で入れない)。how には確かめ方(自動返信メールが来た・画面の保存で完了表示を見た 等)。
    company と form は formlist_unsure / formlist_log に出ているとおりに入れる。"""
    code, d = api("POST", "/api/outcome", {"company": company, "form": form, "delivered": delivered, "how": how})
    return d


@mcp.tool()
def formlist_reply_add(company: str, kind: str, text: str, form: str = "", hp: str = "", memo: str = "") -> dict:
    """相手から返事が来たら記録する。kind は 興味あり／資料がほしい／金額を知りたい／時期未定／担当違い／不要／その他。text は返事の原文。
    「不要」は、同じ操作で送らないリストに入る(hp か form のアドレスが要る)。返り値の next に、次にすることが入っている。
    返事はその日のうちに返すのが決まり。返す文面は利用者に全文を見せ、利用者が自分のメールから送る(このツールからは送らない)。"""
    code, d = api("POST", "/api/reply", {"company": company, "kind": kind, "text": text, "form": form, "hp": hp, "memo": memo})
    return d


@mcp.tool()
def formlist_manual_kit(file: str) -> dict:
    """自動で送れなかった会社(画像認証がある等)に、手で送るための一式(フォームのアドレス・件名・置き換え済みの全文)を出す。送るのは利用者の手。
    利用者が送ったら、1社ずつ formlist_manual_sent で記録する(記録しないと、二重送信の防止と1日の上限に数えられない)。legal_missing に項目があれば、送る前に署名を直す。"""
    code, d = api("GET", f"/api/manual_kit?file={urllib.request.quote(file)}")
    return d


@mcp.tool()
def formlist_manual_sent(company: str, form: str, how: str = "") -> dict:
    """利用者が手でフォームから送ったことを記録する。利用者が「送った」と言った会社だけに使う。"""
    code, d = api("POST", "/api/manual_sent", {"company": company, "form": form, "how": how})
    return d


@mcp.tool()
def formlist_report(days: int = 7) -> dict:
    """営業の報告(期間内): 送った社数・結果の内訳・届いたと確かめた数・返事の数と種類・届いたか確かめが要る会社・14日たって返事の無い会社(別の窓口へ1回まで連絡してよい)。
    件数が少ないうちは割合を出さない。毎週1回、利用者に報告する。"""
    _, d = api("GET", f"/api/report?days={int(days)}")
    return d


@mcp.tool()
def formlist_opens() -> dict:
    """会社ごとに、提案書のリンクが開かれたかを返す(送信元の資料URLに {識別} を入れて送った会社が対象)。
    status: 開いた合図あり(JavaScriptが動いた)／開いた(送信直後・自動確認の可能性)／触れただけ(自動確認の可能性)／未開封／未送信。
    「開いた」は人が必ず開いた証明ではない(JavaScriptを動かす自動確認も出しうる)。読んだかも分からない。運用者が送信後に自分で試した分も数える。
    開いた会社は返事が来る見込みが高いので、毎週の報告に添える。開いたのに連絡の無い会社への送り直しは、
    送信の関門(同じ会社は14日で2回まで)の範囲で、文面を利用者に全文見せてから。通信できないときは理由が返る(0件と取り違えない)。"""
    code, d = api("GET", "/api/opens")
    return d


@mcp.tool()
def formlist_log(limit: int = 30) -> dict:
    """送信の記録（練習・本番）。結果の意味: 練習OK=送っていない／送信済み／手で送る必要あり=フォームを開いて手で送る／
    送れたか確認が要る=送信ボタンは押した。相手の自動返信メールで確かめる。"""
    _, rows = api("GET", "/api/log")
    return {"total": len(rows), "recent": rows[-limit:][::-1]}


if __name__ == "__main__":
    mcp.run()
