"""AIまるなげ営業(フォームリスト作成ツール)をブラウザで使う。実行環境: Windows / Python 3.9+。標準ライブラリ中心。
起動: py app.py （ブラウザが自動で開く）。自分のPCの中だけで動く（127.0.0.1）。外には公開しない。
できること: 1 探す（エリア・業種・ポータルURL・NGリスト） 2 結果を見る 3 送る（送る内容を出す→画面の外の確認で承認→本番。文面編集・送信元の保存・送信履歴）
中身は formlist.py と autosend.py をそのまま呼ぶ。画面はその進み具合を映す。送る画面の状態(s0〜s7)はサーバーが決めて /api/meta の send_state で返す。
安全: 本番送信は、送る内容(送る会社と全文)を出したあと、画面の外の確認ダイアログで人が「はい」を押したときだけ動く。
     送る会社と全文は autosend.build_plan の1か所で決め、確認番号で本番と照合する。練習(試走)の道は無い。"""
import csv
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote

import time
import filelock
import ng_list
import approval
import channel_choice
import message_adoption
import license_check
import bug_report
import proposal_links
import sales_records
import send_rules
import osm_source
import philosophy
import copy_guard
import ports
import settings_dir
import settings_guard
import state
import updater
import ai_engine
import muse_link
from ng_state_store import NgStateStore

HERE = Path(__file__).resolve().parent
OUT = HERE / "出力"
SETTINGS = settings_dir.setup(HERE)  # 設定(送信元・文面・採用記録・計測キー)の置き場を決め、初回だけ本体のフォルダから写す(Q899 第2段)。コピーが何個あっても設定は1つ
MSG = settings_dir.root() / "文面"
SENDER = settings_dir.root() / "sender.json"
# 先に読み込んだ部品の置き場も、決まった置き場にそろえる(移行の照合に失敗して本体のフォルダで動くとき、計測キー・パターンだけ別の場所を読まない R2)
proposal_links.KEY_FILE = settings_dir.root() / "計測キー.txt"  # patterns は関数の中で後から読み込むので、決まった置き場をそのまま使う
LOG = OUT / "送信ログ.csv"
IS_DEV = (HERE / "開発版_自動更新しない.txt").exists()  # 開発用の作業フォルダの目印(公開物には含めない)。ある場所だけ「開発用」として動く
DEV_PORT = 8785  # 開発用の既定の番号。お客様版の8780〜8782(更新の確認・同時検索が使う番号)とぶつけない
PORT = int(os.environ.get("FORMLIST_PORT") or os.environ.get("FORMLIST_ENTRY_PORT") or (DEV_PORT if IS_DEV else 8780))  # FORMLIST_ENTRY_PORT: 入口の番号だけを変える(試験用。FORMLIST_PORT と違い、導入先への振り向けは止めない)  # 複数の検索を同時に走らせるとき、MCP が8781以降で別のサーバーを立てる。画面で使うのは8780(開発用は8785)
assert 8780 <= PORT <= 8790, PORT
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PY = sys.executable[:-len("pythonw.exe")] + "python.exe" if sys.executable.lower().endswith("pythonw.exe") else sys.executable
JOB = {"lines": [], "running": False, "name": ""}
SJOB = {"lines": [], "running": False, "name": ""}
HUMAN = {"asking": 0, "last_action": 0.0}  # 人の操作中の印(承認の窓を出している数・最後の書き込みの時刻)。whoami で見張りに渡す  # 「探す」専用の枠。送る系(確認・本番送信・手入力)と同時に動かせる(2026-10-30 運用者の指示「ほかの処理が動いていても同時に動くように」)
OPENER_COL = "書き出しの作り方"  # リストの列。画面はこの値だけを見て「どう作った書き出しか」を言う(作り方と説明を一致させる)
BASIS_COL, BASIS_URL_COL, BASIS_WHY_COL = "書き出しの根拠", "根拠URL", "根拠が拾えなかった理由"  # リストの列。根拠の文・取得元・拾えなかった理由(人が後から確かめられるように残す)
OPENER_SOURCES = {"philosophy": "ホームページの特徴と文面の提案から作った下書き",
                  "title": "ホームページの題から作った下書き", "general": "会社名と業種から作った下書き",  # 旧版が作った行の表示用。いまは新しく作らない
                  "fallback": "書き出しは自動の下書き(ホームページから特徴が取れなかったため、業種の文面に沿った型を入れています。直せます)",
                  "ai": "AI(Claude)が書き直した下書き", "person": "あなたが直した書き出し"}
_SOURCE_OF = {v: k for k, v in OPENER_SOURCES.items()}


def opener_draft_for(overview="", industry="", basis="", template=None, taken=()):
    """(下書き, 作り方)。ホームページの本文から拾った方針・思いの文(basis)の引用だけで作る(philosophy.opener_from_basis)。
    拾えなかった会社は ("", "")。題だけ・会社名と業種だけの下書きは作らない(2026-11-02 運用者の指示「ぜんぜんだめ」)。overview・industry は旧版との呼び出し互換のため受けるだけ"""
    if template is None:  # この会社に実際に送る文面(業種で選ばれる文面)の提案を、第2文に使う
        import autosend
        try:
            template = autosend.pick_message_source(str(MSG), industry)[1] if MSG.exists() else ""
        except OSError:
            template = ""
    d = philosophy.opener_from_basis(basis, template, taken)  # taken=他の会社の書き出し。2文とも同じ行を作らない
    if d:
        return d, "philosophy"
    d = philosophy.fallback_opener(template)  # 前提が取れない会社も、空欄で止めずに自動の下書きを入れる(印で分かる)
    return (d, "fallback") if d else ("", "")


def opener_why(row):
    """書き出しが空の会社について、なぜ空かを人に言う1文。書き出しがあれば空"""
    if (row.get("書き出し") or "").strip():
        return ""
    if (row.get(BASIS_COL) or "").strip():
        return "ホームページの文は拾えましたが、お客様・過ごす時間・場面(癒しの時間、ご家族連れ、施術中の過ごし方など)が読み取れず、送る文面の提案とつなげられませんでした(拾った文は「書き出しの根拠」の列にあります。手で書くか、AIに書き直してもらえます)"
    return (row.get(BASIS_WHY_COL) or "").strip() or "この会社のホームページから方針・思いを拾う前のリストです(もう一度「探す」で作り直すと拾います)"


def opener_source(row):
    """リストの行の書き出しの作り方(title/general/ai/person)。列が無い・空の古い行は ""(どう作ったか分からない)"""
    return _SOURCE_OF.get((row.get(OPENER_COL) or "").strip(), "")


APPROVED = {"key": None}  # 画面で人が「この内容で送ることを承認する」を押した印。AI(MCP)からの本番送信に要る
PREVIEW = {"key": None, "names": [], "parts": {}}  # 本番の前に「送る内容を出す」を済ませた印と、その確認に出た会社名・変わったものを言うための材料。本番を始めたら消す
SEND_INFO = {"total": 0, "started": "", "dismissed": True}  # 直近の本番の社数と始めた時刻(送っている・送り終えたの数を出す)。dismissed=送り終えた表示を閉じた
LOCK = threading.Lock()
_NG_STORE = None
TOKEN = secrets.token_urlsafe(24)  # 起動のたびに変わる。他サイトの画面からの操作を断るためのもので、同じPC上のプロセスは画面を読めば取れる
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
UI_COOKIE = "formlist_ui"
UI_SESSION = secrets.token_urlsafe(24)  # 画面(ブラウザ)の読み込みでだけ渡す合言葉。HttpOnly なので画面のスクリプトからは読めず、承認の呼び出しに付くのはブラウザが自動で付けるときだけ
GOOGLE_PLACES_TEXT_BLOCK = "Google Places Text Search is not connected. API terms, billing, and storage/export approval are unverified, so this UI stops before starting a job and does not auto-fallback."


BUILD = updater.build()  # 読み込んだ時点の、全ファイルの印。ディスクの印と違えば、更新したあとで起動し直していない
UPDATE_BUSY = threading.Lock()
UPDATE_CACHE = {}  # {"at": 時刻, "result": 照合の結果}。5分は取り直さない(画面を開くたびに公開側へ問い合わせない)
VERSION = ports.disk_version(HERE) or hashlib.sha256(b"".join((HERE / f).read_bytes() for f in ports.VERSION_FILES)).hexdigest()[:12]  # 見張りも同じ式で比べる(ports.disk_version)。古い版が動き続けていないかをMCPが照合する(読み込み済みの部品も含める)


def _pythonw():
    exe = sys.executable
    return exe[:-len("python.exe")] + "pythonw.exe" if exe.lower().endswith("python.exe") else exe


def installed_dir(environ=None):
    """導入先のコピーのフォルダ。定義は ports.installed_dir(起動の入口すべてが同じ決め方を使う R26)"""
    return ports.installed_dir(environ)


def launch_target(here=None, environ=None):
    """起動の入口がどのコピーを起動するか: (導入先 or None, 理由)。定義は ports.launch_target(install.ps1 -Open も同じ関数を呼ぶ R26)"""
    return ports.launch_target(here or HERE, environ)


EXIT_SHOWN = 10  # 起動を自分で止め、理由を表示した(作業中の別コピー・別のアプリが番号を使っている)。振り向けた側は、この終了コードのときだけ黙って終わる


def launch_notice(msg, show=True):
    """起動を止めた理由を、黙って終わらせずに残す。出力/app_起動ログ.txt へ1行。画面から起動したとき(show)は、Windows の通知の窓にも出す"""
    try:
        OUT.mkdir(exist_ok=True)
        with open(OUT / "app_起動ログ.txt", "ab") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 起動 port={PORT} {msg}\n".encode("utf-8"))
    except OSError:
        pass
    print(msg)
    if show and os.name == "nt" and not os.environ.get("FORMLIST_NO_DIALOG"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "AIまるなげ営業", 0x40 | 0x10000)  # 情報の印・最前面
        except Exception:
            pass


def watch_status():
    """見張りの状態(画面の隅に出す・R4)。見張りが無い版・読めないときは None"""
    try:
        import watchdog
        return watchdog.status(HERE)
    except Exception:
        return None


def ensure_watchdog_task():
    """導入先のコピーとして起動したときだけ、見張りの登録を確かめ、無い・前の版なら登録し直す(自動更新で届いた既存の利用者にも効かせる・R4の自己修復)"""
    if os.name != "nt" or IS_DEV or os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("FORMLIST_NO_TASK"):
        return
    try:
        if installed_dir().resolve() != HERE.resolve():
            return
        import watchdog
        watchdog.ensure_task(HERE)
    except Exception as e:
        launch_notice(f"見張りの登録を確かめられませんでした({type(e).__name__})", show=False)


def watchdog_registered():
    """見張り(5分ごとの確認)がこのPCに登録されているか。確かめられないときは False(約束しない側に倒す)"""
    try:
        import watchdog
        return watchdog.task_has_watchdog()
    except Exception:
        return False


def replace_other_copy(port):
    """別のフォルダの、このツールの古いコピーを止める(何も動いていないときだけ)。止められたら True"""
    j = ports.job(port)
    if j is None or j.get("running") is not False:  # 作業中・作業中か分からない(取得失敗・形が違う・項目が無い)ときは止めない(R1・R10)。古い版は ports.job が /api/job の running を読む
        return False
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/", timeout=5) as r:
            m = re.search(r"const TOKEN='([^']+)'", r.read().decode("utf-8"))
        if not m:
            return False
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", data=b'{"if_idle": true}', method="POST",
                                     headers={"X-Token": m.group(1), "Content-Type": "application/json"})
        opener.open(req, timeout=5).read()
    except Exception:
        return False
    for _ in range(20):
        time.sleep(0.5)
        if ports.can_bind(port):
            return True
    return False


def peer_log(msg):
    """同時検索の振り分け・断りを1行ずつ残す(何が起きたか後から追えるように。出力フォルダの app_起動ログ.txt)"""
    try:
        OUT.mkdir(exist_ok=True)
        cap_log(OUT / "app_起動ログ.txt")
        with open(OUT / "app_起動ログ.txt", "ab") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 同時検索 port={PORT} {msg}\n".encode("utf-8"))
    except OSError:
        pass


def start_peer():
    """別の条件で同時に探すために、もう1つ画面のサーバーを別の番号で立てる。(番号, None) か (None, 理由) を返す。
    先に空いている番号に立てる。空きが無ければ、何も動いていない自分のサーバーを使う。3つとも動いていれば断る"""
    pt, why, _ = start_peer_kind()
    return pt, why


def start_peer_kind():
    """start_peer と同じ。失敗のときは (None, 理由, 種類) を返す。種類: start_failed(立ち上げ待ち切れ) / all_busy(3つとも使用中) / other_copy(別の場所の同じツールが番号を使用) / port_taken(別のアプリが番号を使用)"""
    here = str(HERE)
    seen = {}
    for pt in ports.POOL:
        if pt != PORT:
            seen[pt] = ports.state(pt, here)
    for pt, st in seen.items():
        if st != "free":
            continue
        OUT.mkdir(exist_ok=True)
        cap_log(OUT / "app_起動ログ.txt")
        with open(OUT / "app_起動ログ.txt", "ab") as log:
            subprocess.Popen([_pythonw(), "-X", "utf8", str(HERE / "app.py")], cwd=HERE,
                             env=dict(os.environ, FORMLIST_NO_BROWSER="1", FORMLIST_PORT=str(pt), PYTHONIOENCODING="utf-8"),
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=NO_WINDOW,
                             start_new_session=(os.name != "nt"))  # Mac/Linux: 親が終わっても画面のサーバーを道連れにしない
        for _ in range(40):
            time.sleep(0.5)
            if ports.state(pt, here) == "ours":
                return pt, None, ""
        return None, f"{pt}番の画面を立ち上げられませんでした。少し待ってからもう一度押してください(遅いパソコンでは、立ち上がりが遅れることがあります)。何度やっても同じなら、出力フォルダの app_起動ログ.txt の末尾を担当者に送ってください", "start_failed"
    for pt, st in seen.items():
        j = ports.job(pt) if st == "ours" else None
        if j is not None and not j.get("running"):
            return pt, None, ""
    if any(st == "ours" for st in seen.values()):
        return None, f"同時に探せるのは{len(ports.POOL)}つまでです（パソコンが重くなり、Googleにも止められやすくなるため）。どれかが終わってから、もう一度押してください", "all_busy"
    if any(st == "other_copy" for st in seen.values()):
        return None, "別の場所に入っている、このツールの別のコピーが、同時検索用の番号を使っています。その画面を閉じてから、もう一度お試しください。どの画面か分からないときは、パソコンを再起動してください", "other_copy"
    return None, "別の画面を立てる番号（" + "・".join(str(x) for x in ports.POOL) + "）が、ほかのアプリに使われています。使っているアプリを閉じるか、パソコンを再起動してから、もう一度お試しください", "port_taken"


HANDOFF_KEEP_SEC = 600
HANDOFF_ID = re.compile(r"^[0-9a-f]{16}$")


def handoff_sweep():
    """受け取られないまま10分たった受け渡しを消す(検索条件にはNGリストなどが入る)"""
    now = time.time()
    for f in OUT.glob("peer_handoff_*"):
        try:
            if now - f.stat().st_mtime > HANDOFF_KEEP_SEC:
                f.unlink()
        except OSError:
            pass


def handoff_write(d):
    """別枠の画面へ渡す検索条件を、出力フォルダ(全サーバー共通)へ一時保存して受け渡し番号を返す。
    ポートをまたぐ直接の呼び出しは、宛先のトークン検査(guard)で必ず断られるため、この方式にしている。10分たったものは消す"""
    OUT.mkdir(exist_ok=True)
    handoff_sweep()
    hid = secrets.token_hex(8)
    (OUT / f"peer_handoff_{hid}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return hid


def handoff_take(hid):
    """受け渡し番号から検索条件を1回だけ取り出す(取り出したら消す)。無い・古い・形が違うときは None"""
    if not HANDOFF_ID.match(str(hid or "")):
        return None
    handoff_sweep()
    f = OUT / f"peer_handoff_{hid}.json"
    claim = OUT / f"peer_handoff_{hid}.claim"
    try:
        os.close(os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY))  # 取り出す権利を1つのタブだけに与える(ファイル名の排他的な作成は、同時でも勝者が1つに決まる。os.replace は同時だと両方成功に見えた)
    except OSError:
        return None
    try:
        if time.time() - f.stat().st_mtime > HANDOFF_KEEP_SEC:
            f.unlink()
            return None
        d = json.loads(f.read_text(encoding="utf-8-sig"))
        f.unlink()
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        try:
            claim.unlink()
        except OSError:
            pass


def google_block_until():
    """Googleに止められた印(formlist.py が書く 出力/google_止められた.json)がまだ有効なら、終わる時刻('YYYY-MM-DDTHH:MM:SS')。無ければ空"""
    import datetime as dt
    try:
        until = json.loads((OUT / "google_止められた.json").read_text(encoding="utf-8-sig"))["until"]
        return until if dt.datetime.fromisoformat(until) > dt.datetime.now() else ""
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def google_block_message(until):
    return f"Googleに止められたため、{until[11:16]}ごろまで、Googleマップを使う新しい検索は始められません。終わった検索の結果は、そのまま見られます。時間がたってから、もう一度押してください"


def history_status(d):
    """この検索条件に、前回の続き(探索履歴)があるか。formlist.py が履歴の組を作るときと同じ作り方(業種の正規化・キーワードの分け方)で鍵を作る。
    戻り: state(match=続きあり / none=記録なし / similar=同じ場所で別の組み合わせ / not_google=Googleマップを使わない / empty=条件がまだ足りない) と、画面に出す1行(text)"""
    import industries
    import search_history
    if "google" not in (d.get("sources") or ["google"]):
        return {"state": "not_google", "text": "Googleマップを使わない探し方では、前回の続きは使えません。いつも最初から集めます"}
    area = str(d.get("area") or "").strip()
    inds = list(dict.fromkeys(industries.canonical(i) for i in (d.get("industries") or []) if not industries.retired_reason(i)))
    kws = [k for k in re.split(r"[\s、,，]+", str(d.get("keywords") or "")) if k]
    if not area or not (inds or kws):
        return {"state": "empty", "text": ""}
    key = search_history.group_key(area, inds, kws)
    e = search_history.load(OUT, key)
    if e:
        n, done, total = len(e.get("checked") or {}), int(e.get("jobs_done") or 0), int(e.get("n_all") or 0)
        return {"state": "match", "checked": n, "jobs_done": done, "n_all": total, "updated": e.get("updated", ""),
                "text": f"前回の続きから集めます(前回までに {n}社を調べ済み・検索は全{total}件のうち{done}件まで終えています)"}
    why = search_history.why_no_history(OUT, key)
    return {"state": "similar" if why.startswith("同じ場所") else "none", "checked": 0, "text": why}


SEARCH_COND = {}


def cond_key(d):
    """動いている検索の条件の鍵(履歴の組と同じ作り)。場所と、業種・キーワードが同じなら同じ検索とみなす"""
    import industries
    import search_history
    inds = list(dict.fromkeys(industries.canonical(i) for i in (d.get("industries") or []) if not industries.retired_reason(i)))
    kws = [k for k in re.split(r"[\s、,，]+", str(d.get("keywords") or "")) if k]
    area = str(d.get("area") or "").strip()
    return search_history.group_key(area, inds, kws) if area and (inds or kws) else ""


def busy_reply(d):
    """この画面の「探す」が動いているときの返事。空いている別枠を用意して、その画面へ検索条件を渡す(利用者は「探しはじめる」を押すだけでよい)。
    別枠を用意できなければ、理由ごとの断り文(kind つき)を返す"""
    mine = cond_key(d)
    for pt0 in [PORT] + [x for x in ports.POOL if x != PORT]:  # 二度押し対策: 動いている検索(この画面・別枠)と同じ条件なら、別枠で並べて走らせない(重複・Googleへのアクセス倍増・履歴の競合のため)
        w = {"cond": SEARCH_COND.get("key", "") if SJOB["running"] else ""} if pt0 == PORT else (ports.job(pt0) or {})
        if mine and w.get("cond") == mine:
            peer_log(f"search 断り 種類=same_condition 枠={pt0}")
            return {"started": False, "kind": "same_condition", "error": f"この条件の検索は、すでに{'この画面' if pt0 == PORT else '検索' + str(ports.POOL.index(pt0) + 1)}で動いています。終わるまで待つか、別の条件にしてから押してください"}
    pt, why, kind = start_peer_kind()
    if pt is None:
        peer_log(f"search 断り 種類={kind}")
        return {"started": False, "error": why, "kind": kind}
    hid = handoff_write(d)
    peer_log(f"search 別枠へ振り分け 枠={pt}")
    return {"started": False, "peer": {"url": f"http://127.0.0.1:{pt}/?peer=1&handoff={hid}", "port": pt, "slot": ports.POOL.index(pt) + 1}}


def send_stop_flag():
    """送信中の「ここで止める」の印。送る処理(autosend)が1社ごとに見て、あれば次の会社に進まない"""
    return OUT / f"送信_止める_{PORT}.flag"


def _rm(p):
    try:
        Path(p).unlink()
    except OSError:
        pass


SEND_MARK = f"送信中_記録_{PORT}.json"  # 本番を始めたときに書き、終わったら消す。番号ごとの別ファイル(別の番号の送信の記録を上書きしない)。残っていれば、前回は途中で落ちた(次の起動で知らせる)
SEND_MARK_GLOB = "送信中_記録*.json"  # 古い名前(番号なし)の記録も拾う


def _pid_alive(pid):
    """そのプロセスが今も動いているか。Windows の os.kill(pid, 0) は相手を終わらせてしまうので使わない"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _rm_own_mark():
    """送信中_記録.json を消す。自分(この番号・このプロセス)が書いた記録だけ。別のサーバーが書いた記録は、そのサーバーが終えるまで残す"""
    f = OUT / SEND_MARK
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(d, dict) and d.get("port") == PORT and d.get("pid") == os.getpid():
        _rm(f)


def keep_awake_start():
    """送信の間、パソコンが眠らないようにする(外部の部品なし)。Windows は SetThreadExecutionState、Mac は caffeinate。
    戻り: (止めるときに呼ぶ関数, "on" か "none")。取れなくても送信は止めない(画面で案内だけ出す)。
    Windows の指定は、呼んだスレッドが生きている間だけ効く。run_job の work() は送信が終わるまで生きているので、そこで呼ぶ"""
    try:
        if os.name == "nt":
            import ctypes
            f = ctypes.windll.kernel32.SetThreadExecutionState
            if f(0x80000001):  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                return (lambda: f(0x80000000)), "on"
        elif sys.platform == "darwin" and shutil.which("caffeinate"):
            c = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return (lambda: c.terminate()), "on"
    except Exception:
        pass
    return (lambda: None), "none"


def run_job(name, args, stdin_text=None, on_ok=None, extra_env=None):
    slot = SJOB if name == "探す" else JOB
    with LOCK:
        if slot["running"]:
            return False
        slot.update(lines=[], running=True, name=name, code=None, proc=None, fresh=False, started=time.strftime("%Y-%m-%dT%H:%M:%S"), awake="")
        if name == "本番送信":
            _rm(send_stop_flag())  # 前回の止める印を持ち越さない
            try:
                OUT.mkdir(exist_ok=True)
                (OUT / SEND_MARK).write_text(json.dumps({"total": SEND_INFO.get("total", 0), "started": SEND_INFO.get("started", ""), "port": PORT, "pid": os.getpid()}), encoding="utf-8")
            except OSError:
                pass

    def work():
        code = None
        release = None
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONWARNINGS="ignore", **(extra_env or {}))
            if name == "本番送信":
                env["FORMLIST_SEND_STOP_FLAG"] = str(send_stop_flag())
                release, slot["awake"] = keep_awake_start()
            p = subprocess.Popen([PY, "-X", "utf8", *args], cwd=HERE, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                 errors="replace", creationflags=NO_WINDOW)
            slot["proc"] = p
            if name == "本番送信":
                try:
                    mf = OUT / SEND_MARK
                    md = json.loads(mf.read_text(encoding="utf-8"))
                    md["child"] = p.pid
                    mf.write_text(json.dumps(md), encoding="utf-8")
                except (OSError, ValueError):
                    pass
            if stdin_text is not None:
                p.stdin.write(stdin_text)
            p.stdin.close()
            for line in p.stdout:
                slot["lines"].append(line.rstrip())
            code = p.wait()
            slot["lines"].append(f"（終了 コード{code}）")
        except Exception as e:
            slot["lines"].append(f"（処理を始められませんでした: {e!r}）")
        finally:
            if release:
                try:
                    release()
                except Exception:
                    pass
            if name == "本番送信":
                _rm(send_stop_flag())
                _rm_own_mark()  # 生きて終えた。画面が「残り」を出せるので、起動時の知らせは要らない(消すのは自分が書いた記録だけ)
            with LOCK:  # 確認の印を立てるのも、動いている印を下ろすのも、同じ排他の内側で行う
                try:
                    if code == 0 and on_ok:
                        on_ok()
                finally:
                    slot.update(running=False, code=code, proc=None, fresh=True)

    threading.Thread(target=work, daemon=True).start()
    return True


def preview_names(lines):
    """確認(--preview)の出力に出た、送る会社の名前。承認ダイアログに出す"""
    out = []
    for line in lines:
        m = re.match(r"===== 送る文面 (.+?)（業種", line)
        if m:
            out.append(m.group(1))
    return out


def set_preview(key, parts=None, channel="form"):
    PREVIEW["channel"] = channel
    PREVIEW["key"] = key
    PREVIEW["names"] = preview_names(JOB["lines"])
    PREVIEW["parts"] = dict(parts or {})
    APPROVED["key"] = None


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def outputs():
    OUT.mkdir(exist_ok=True)
    files = [p for p in OUT.glob("フォームリスト_*.csv") if p.stat().st_size > 0]
    return [p.name for p in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)]


def sample_rows_for(rows, industry):
    """見本に使う会社の候補(送るリストの行)。実際に送るとき(autosend.pick_message_source)と同じく、
    業種は新しい分類の名前にそろえて(canonical)比べる。古い業種名で入ったリストでも、その業種の会社が当たる。
    その業種の会社がリストに無ければ空を返す(別の業種の会社で見本を作らない)。industry が空・'default' なら全行"""
    import industries
    want = industries.canonical(industry) if industry and industry != "default" else ""
    if not want:
        return list(rows)
    return [r for r in rows if industries.canonical(str(r.get("業種") or "")) == want]


def messages():
    MSG.mkdir(exist_ok=True)
    if not (MSG / "default.txt").exists():
        src = HERE / "文面ひな形" / "default.txt"
        (MSG / "default.txt").write_text(src.read_text(encoding="utf-8") if src.exists() else "{企業名} ご担当者さま\n", encoding="utf-8")
    return {p.stem: p.read_text(encoding="utf-8-sig") for p in sorted(MSG.glob("*.txt"))}


def message_info():
    """業種ごとに、専用の文面があるか(無ければ「標準」が使われる)。新しい分類に無い名前の文面(古い業種名で作ったもの)は orphans に出す"""
    import industries
    have = {p.stem for p in MSG.glob("*.txt")} if MSG.exists() else set()
    names = list(osm_source.INDUSTRY_TAGS)
    import patterns  # 専用の文面が無い業種に使われる、割り当て済みのパターン(画面に中身を出す)
    pat = {n: patterns.pattern_for(n) for n in names if n not in have and patterns.pattern_for(n)}
    texts = {n: patterns.text_for_industry(n) for n in pat}
    texts = {n: t for n, t in texts.items() if t}  # 割り当てはあっても、パターンのファイルが無い環境(公開版)では標準が使われる
    # uses: 画面の1つの印。その業種に「業種ごとの文面」(業種名.txt か、割り当て済みのパターン)が使われるか、「標準」か(D-8。専用とパターンを分けて見せない)
    uses = {n: ("業種ごと" if (n in have or n in texts) else "標準") for n in names}
    import autosend
    mt = max([f.stat().st_mtime for f in MSG.glob("*.txt")] or [0]) if MSG.exists() else 0  # 画面が「文面が変わったか」を見分け、保存先と最終更新を利用者に見せる
    return {"msg_dir": str(MSG), "msg_mtime": time.strftime("%m/%d %H:%M:%S", time.localtime(mt)) if mt else "", "expand_on": not (MSG / autosend.EXPAND_OFF).exists(), "industries": {n: ("専用" if n in have else "標準") for n in names}, "uses": uses,
            "patterns": {n: pat[n] for n in texts}, "pattern_texts": texts,
            "pattern_status": patterns.pattern_status(), "patterns_missing": patterns.patterns_missing(),  # パターン番号ごとの有無(欠けている番号は、画面の保存先の表示で知らせる)
            "subjects": {n: patterns.subject_for_industry(n) for n in names if patterns.subject_for_industry(n)},  # 業種の型ごとの件名(文面の画面で同時に直せる)
            "orphans": [{"name": n, "to": industries.LEGACY.get(n, "")} for n in sorted(have) if n != "default" and n not in names and not n.startswith("承認")]}


GREETING = "{企業名} ご担当者さま"
LINK_MARK = "《資料のリンク》"  # 文面の画面で、資料のリンクの位置に見せる印(記号 {資料URL} は見せない。保存のときに戻す)
_MARKS = {"{企業名}": "《相手の会社名》", "{書き出し}": "《会社ごとの書き出し》", "{署名}": "《署名》", "{資料URL}": LINK_MARK}


def split_message(text):
    """文面ファイルを、画面の3つの部分に分ける {greeting, opener, body, signature, split_ok}。
    greeting=1行目が宛名の行 / opener=宛名の下が {書き出し} だけの行 / signature=最後が {署名} だけの行。body は残りで、{資料URL} は《資料のリンク》にする。
    ほかの位置に記号が残る古い文面は split_ok=False で、残った記号も《 》の印にして body に出す(画面は「位置がほかの文面と違います」と言う)"""
    lines = (text or "").replace("\r\n", "\n").strip("\n").split("\n")
    greeting = bool(lines) and lines[0].strip() == GREETING
    if greeting:
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    opener = bool(lines) and lines[0].strip() == "{書き出し}"
    if opener:
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    signature = bool(lines) and lines[-1].strip() == "{署名}"
    if signature:
        lines = lines[:-1]
    body = "\n".join(lines).strip("\n")
    split_ok = not any(tok in body for tok in ("{企業名}", "{書き出し}", "{署名}"))
    for tok, mark in _MARKS.items():
        body = body.replace(tok, mark)
    return {"greeting": greeting, "greeting_line": GREETING if greeting else "", "opener": opener, "body": body, "signature": signature,
            "split_ok": split_ok, "layout_ok": split_ok}


def join_message(parts):
    """画面の部分から文面ファイルの形に組み立てる(autosend.render が読む形は今のまま)。宛名の行 → {書き出し} → 本文 → {署名} の順。
    本文の《 》の印は記号に戻す。印を消した部分は入らない(資料のリンクを消した文面は、資料URLを要らない文面になる)"""
    body = str(parts.get("body") or "").replace("\r\n", "\n").strip("\n")
    for tok, mark in _MARKS.items():
        body = body.replace(mark, tok)
    blocks = ([GREETING] if parts.get("greeting", True) else []) + (["{書き出し}"] if parts.get("opener", True) else []) + ([body] if body else []) \
        + (["{署名}"] if parts.get("signature", True) else [])
    return "\n\n".join(blocks) + "\n"


# 送る内容の「共通の文」で、会社ごとに変わる部分に置く印(画面は marks の文字列をそのまま探して色つきの札にする。{ } 【 】は出さない)
MARK_WORDS = ("{企業名}", "{書き出し}", "{資料URL}")  # 画面(U3)がこの記号を色の札に変えて見せる(サーバーは置き換えない)


def marked_text(template, sender):
    """共通の文。会社ごとに変わる部分は印に、署名は送信元の実物にする(autosend.render と同じ置き換えの順・形)"""
    text = template.replace("{署名}", str(sender.get("署名", "")))
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def marked_subject(sender):
    return str(sender.get("件名", ""))


def short_key(digest_value):
    """画面に見せる確認番号の頭12文字(64文字の番号は出さない)。承認のときに、画面が見た送る内容と同じかの照合に使う"""
    return (digest_value or "")[:12] or None


def count_error():
    """E-12。社数が1以上の整数でない(上限は無い。空欄は全社)"""
    return {"error": "1以上の社数を入れてください。空欄にすると、対象の全社に送ります", "field": "max"}


def fingerprint():
    """文面と送信元の中身。確認したあとに変わったら、確認をやり直させる"""
    h = hashlib.sha256()
    for f in sorted(MSG.glob("*.txt")):
        h.update(f.name.encode("utf-8") + b"\0" + f.read_bytes())
    import patterns  # 割り当て済みのパターンの中身も、確認したあとに変わったら確認をやり直させる対象
    for f in sorted(patterns.PATTERN_DIR.glob("*.txt")):
        h.update(f.name.encode("utf-8") + b"\0" + f.read_bytes())
    if SENDER.exists():
        h.update(SENDER.read_bytes())
    return h.hexdigest()


def default_ng_state_db():
    return state.DB.with_name(state.DB.stem + "_ng" + state.DB.suffix)


def init_ng_store(db_path=None):
    """Connect the UI to the NG/state DB. Tests pass a temp path; normal startup uses a sidecar DB."""
    global _NG_STORE
    _NG_STORE = NgStateStore(db_path or default_ng_state_db())
    _NG_STORE.migration_status()
    return _NG_STORE


_NG_SYNC = False  # 画面を起動したときだけ True。テストは従来どおりの挙動
NG_MIGRATION = {"error": ""}


def ensure_ng_migrated(store, src=None):
    """画面の起動時に1回。テキストの送らない会社(NGリスト.txt)を、バックアップを取ってDBへ移し、状態を canonical にする。
    済むまで、本番の送信は押す直前に止まる(画面・AIからの送信の関門)。失敗しても起動は止めず、理由を NG_MIGRATION に残す"""
    src = Path(src or ng_list.FILE)
    NG_MIGRATION["error"] = ""
    try:
        status = store.migration_status()
        if status == "canonical":
            pass
        elif status == "failed":
            row = store.migration_row()
            store.resume_failed_migration(row["source_path"], row["backup_path"])
        elif status == "unknown":
            src.parent.mkdir(parents=True, exist_ok=True)
            if not src.exists():
                src.write_text("", encoding="utf-8")
            backup = src.with_name(f"NGリスト_移行前_{time.strftime('%Y%m%d_%H%M%S')}.txt")
            store.migrate_ng_txt(src, backup)
        else:
            raise RuntimeError(f"送らない会社の移行が途中の状態です({status})。手で確かめてください")
        store.sync_legacy_txt(src)
    except Exception as e:
        NG_MIGRATION["error"] = f"{type(e).__name__}: {str(e)}"
    return NG_MIGRATION["error"]


def sync_ng_txt(store):
    """テキストのNGに増えた分(送信後に見つかったお断り等)をDBへ取り込む。探す・確認・本番の前に呼ぶ"""
    if not (_NG_SYNC and store is not None):
        return
    try:
        store.sync_legacy_txt(ng_list.FILE)
    except Exception as e:
        NG_MIGRATION["error"] = f"{type(e).__name__}: {str(e)}"


NGMARK = "画面で「送らない」"


def set_ng_store_for_tests(store):
    """実UIハンドラを一時DBで検証するための注入口。Noneなら従来のテキストNGリストを使う。"""
    global _NG_STORE
    old = _NG_STORE
    _NG_STORE = store
    return old


def mailto_address(v):
    """送信ログのURL欄は mailto:アドレス の形。mailto: と ?以降(件名など)を外してアドレスだけにする(アドレスでなければ、そのまま返す)。
    アドレスのまま登録すると、gmail.com のような無料メールのドメイン全体が止まらず、そのアドレス1つだけが止まる"""
    t = str(v or "").strip()
    if t.lower().startswith("mailto:"):
        return t[7:].split("?", 1)[0].strip()
    return t


NG_WHERE = "送らない先: "


def ng_reply_rows(store=None):
    """返事で「不要」と言われた会社の一覧(新しい順)。理由・日付・送らない先・いま実際に送らないリストに入っているか。入っているかは送らないリスト本体で確かめる"""
    out = []
    for r in reversed(sales_records._read(sales_records.REPLY_LOG)):
        if r.get("種類") != "不要":
            continue
        memo = r.get("メモ", "")
        where = memo.split(NG_WHERE, 1)[1].strip() if NG_WHERE in memo else ""
        if where:
            active = bool(store.is_ng([where])) if store is not None else bool(ng_list.is_ng_email(where) if ng_list.is_email(where) else (ng_list.is_ng(where) or ng_list.is_ng_form(where)))
        else:
            active = False
        out.append({"company": r.get("企業名", ""), "when": r.get("日時", ""), "reason": "返事で不要と言われた", "where": where, "active": active})
    return out


def add_ng_item(item, reason, company="", store=None):
    item = mailto_address(item)
    if store is None:
        return ng_list.add(item, reason, company) or ng_list.is_ng(item) or ng_list.is_ng_form(item) or ng_list.is_ng_email(item)
    raw = str(item or "").strip()
    if not raw:
        return False
    if raw.startswith("@"):
        url = raw[1:].strip()
        return bool(store.add_form_ng(url, reason, company) or store.is_ng([url]))
    if ng_list.is_email(raw):
        return bool(store.add_mail_ng(raw, reason, company) or store.is_ng([raw]))
    host = ng_list.domain(raw)
    if not host or "." not in host or host in ng_list.TOO_WIDE:
        return bool(store.is_ng([raw]))
    if ng_list._shared(host):
        url = raw if re.match(r"(?i)https?://", raw) else "https://" + raw.lstrip("/")
        return bool(store.add_form_ng(url, reason, company) or store.is_ng([url]))
    return bool(store.add_domain_ng(raw, reason, company) or store.is_ng([raw]))


def ng_screen(store=None):
    """画面の「送らない」で入れた行だけ(自動で入った行は戻せない対象にしない)"""
    if store is not None:
        return store.screen_ng_lines()
    if not ng_list.FILE.exists():
        return []
    return [l.strip() for l in ng_list.FILE.read_text(encoding="utf-8-sig").splitlines() if NGMARK in l]


def ng_row_hidden(row, store=None):
    if store is not None:
        return store.is_ng([row.get("HP URL") or "", row.get("問い合わせフォームURL 無ければメール") or ""])
    return ng_list.is_ng_row(row)


def ng_add_screen(hp, form, name, store=None):
    if store is None:
        ok = (hp.startswith("http") and ng_list.add(hp, NGMARK, name)) or (form.startswith("http") and ng_list.add_form(form, NGMARK, name))
        if ng_list.is_email(form):
            ok = bool(ng_list.add_mail(form, NGMARK, name)) or ok or ng_list.is_ng_email(form)
        return ok
    try:
        ok = False
        if hp.startswith("http") and store.add_screen_domain_ng(hp, name):
            ok = True
        if form.startswith("http") and store.add_screen_form_ng(form, name):
            ok = True
        if ng_list.is_email(form) and store.add_screen_mail_ng(form, name):
            ok = True  # 連絡先がメールアドレスの会社は、ドメインと合わせてアドレスでも止める(手動で送った会社を両経路で除外する)
        if ok:
            return True
        return (hp and store.is_ng([hp])) or (form and store.is_ng([form]))
    except Exception:
        return False


def ng_undo_screen(line, store=None):
    if store is None:
        return ng_list.remove_line(line)
    return store.undo_screen_ng_line(line) == "ok"


PORTALS_FILE = OUT / "ポータル登録.json"


def load_portals():
    """業種ポータルの登録(名前とURLの一覧)。ファイルが壊れていたら空として扱う(検索自体は止めない)"""
    try:
        data = json.loads(PORTALS_FILE.read_text(encoding="utf-8-sig"))
        return [x for x in data if isinstance(x, dict) and str(x.get("url", "")).strip()]
    except Exception:
        return []


def save_portals(items):
    OUT.mkdir(exist_ok=True)
    PORTALS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def plan(p, supp, mx, store=None, explain=False):
    """送る会社と各社の全文(autosend.build_plan をそのまま呼ぶ。確認(--preview)・本番(--send)と同じ1つの関数で数える。
    同じ会社への間隔・きょうの残りもここで掛かるので、14日以内に送った会社がリストにあっても、確認番号が本番と一致する)"""
    import autosend
    sender = json.loads(SENDER.read_text(encoding="utf-8-sig"))
    return autosend.build_plan(str(p), supp, store, mx, str(MSG), sender, apply_rules=True, explain=explain)


LAST_PLAN = {}  # digest が最後に作った送る内容(送らない会社の数・止まった理由を、同じ1回の数え方から取るため)


def digest(p, supp, mx, store=None):
    """本番で実際に送る会社と各社の全文の確認番号(送る会社が0なら None)。送らないリスト・送信ログ・文面・送信元が変わって送る相手や中身が変われば、値が変わる"""
    pl = plan(p, supp, mx, store, explain=True)  # explain は表示用の理由を足すだけで、確認番号は変わらない
    LAST_PLAN.clear()
    LAST_PLAN.update(pl)
    return pl["digest"]


_COPY = {"at": 0.0, "v": None}
COPY_TTL = 30  # 秒。画面は /api/meta を頻繁に読むが、他のコピーの有無を確かめる(最大3ポート×0.5秒)のは30秒に1回で足りる


def copy_info():
    """二重コピー・設定の置き場の状態(画面の帯が読む)。結果は30秒使い回す。読み取りだけで、失敗しても画面を止めない。"""
    now = time.time()
    if _COPY["v"] is None or now - _COPY["at"] > COPY_TTL:
        try:
            _COPY["v"] = copy_guard.status(HERE, SETTINGS, ports.POOL)
        except Exception:
            _COPY["v"] = {"here": str(HERE), "settings_dir": str(settings_dir.root()), "warnings": []}
        _COPY["at"] = now
    return _COPY["v"]


def read_sender():
    """保存した送信元。一度も保存していなければ None。壊れていれば {}(全部の欄が空として、欄の名前つきで止まる)"""
    if not SENDER.exists():
        return None
    try:
        data = json.loads(SENDER.read_text(encoding="utf-8-sig"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


LIST_FIELD = "list"  # エラーの応答の field。リストの問題と送信元の問題を同じ文で返さない(E-1〜E-3)


def list_or_error(name):
    """送るリストを開く。(Path, None) か (None, (HTTPコード, 応答))。原因ごとに別の文(E-1 選んでいない / E-2 無い / E-3 Excelで開いたまま)"""
    name = str(name or "").strip()
    if not name:
        return None, (400, {"error": "送るリストを選んでください(「送るリスト」の欄)", "field": LIST_FIELD, "fix": "list"})
    p = safe_out(name)
    if not p:
        return None, (400, {"error": f"選んだリスト「{name}」が見つかりません。消したか名前を変えた可能性があります。「2 結果を見る」で選び直してください", "field": LIST_FIELD, "fix": "list"})
    try:
        with open(p, "rb") as f:
            f.read(1)
    except PermissionError:
        return None, (409, excel_error(p))
    return p, None


def excel_error(p):
    """リストのファイルを読み書きできなかった(拒否を実際に受けたときだけ出す)"""
    return {"error": f"リスト「{Path(p).name}」がExcelで開いたままです。Excelを閉じてから、もう一度押してください", "field": LIST_FIELD, "fix": "list"}


def templates_for(p):
    """このリストの会社に使われる文面 {どの文面か: 本文}。業種ごとに autosend と同じ選び方(業種名.txt → 割り当てたパターン → 標準)で選ぶ"""
    import autosend
    messages()  # 標準の文面が無ければ、ひな形から作る(公開版の初回)
    out = {}
    rows = read_csv(p) if p else []
    for ind in dict.fromkeys([""] + [r.get("業種", "") for r in rows]):
        label, text = autosend.pick_message_source(str(MSG), ind)
        out.setdefault(label, text)
    return out


def message_problems(templates):
    """使う文面に残った見本の文字・置き換わらない記号(文面の保存の検査と同じく、正しく置き換わる4つの記号は止めない)"""
    import placeholders
    out = []
    words = placeholders.sample_words()
    for label, text in templates.items():
        for w in placeholders.leftovers(text, words, known_ok=True)[:1]:
            name = "標準の文面" if label == "標準" else f"文面({label})"
            what = f"見本の「{w['word']}」" if w["kind"] == "見本" else (f"置き換わらない「{w['word']}」" if w["word"] else "閉じていない括弧")
            out.append({"field": "文面", "kind": "文面に見本の文字", "template": label, "industry": "default" if label == "標準" else label, "text": f"{name}に、{what}が残っています"})
    return out


def gate_problems(sender, templates):
    """送る前に直すところ(送信元の関門 send_rules.sender_problems と、使う文面の検査)。空なら送る内容を出せる"""
    probs = send_rules.sender_problems(sender, list(templates.values()))
    if sender is None:
        probs += [x for x in send_rules.sender_problems({}, list(templates.values())) if x["kind"] == "空"]
    return probs + message_problems(templates)


def problem_error(probs):
    """止めたときの応答。先頭の問題の1文と、どの欄か(画面がその欄へ移る)"""
    first = probs[0]
    return {"error": first["text"], "field": first["field"] or "送信元", "fix": "message" if first["field"] == "文面" else "sender", "problems": probs}


SETTINGS_NAME = "画面の設定.json"  # 出力/ の中(自動更新の対象外)。前回の最大の社数を覚える


def load_settings():
    try:
        d = json.loads((OUT / SETTINGS_NAME).read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**kw):
    d = load_settings()
    d.update(kw)
    OUT.mkdir(exist_ok=True)
    (OUT / SETTINGS_NAME).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _log():
    """送信ログ(autosend が書く 出力/送信ログ.csv)。OUT から作る(試験が OUT を一時フォルダにすると、ここも一時フォルダになる)"""
    return OUT / LOG.name


def first_time():
    """はじめての人か(送信ログに、送信ボタンを押した記録 send_rules.PRESSED が1件も無い)"""
    if not _log().exists():
        return True
    return not any(r.get("結果") in send_rules.PRESSED for r in read_csv(_log()))


def default_max(first):
    """送る社数の既定。前回に指定した数(画面の設定.json)があればそれ。無ければ None(未指定=対象の全社。固定の数は置かない)"""
    v = load_settings().get("last_max")
    if isinstance(v, int) and v >= 1:
        return v, "last"
    return (None, "first") if first else (None, "default")


def limits():
    return {"hours": list(send_rules.HOURS) if send_rules.ENFORCE_WINDOW else [],
            "sec_per_company": [30, 60], "window": send_rules.window_refusal()}


SUPPORT_NAME = "サポート記録.txt"


_SID_LAST = {"base": "", "n": 0}
_SID_LOCK = threading.Lock()


def is_disconnect(exc):
    """相手(ブラウザ)が途中で閉じた・切れた失敗。ツールの不具合ではないので、サポート記録に残さない(導入先の記録64件中64件がこれだった)"""
    if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
        return True
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in (10053, 10054, 10058, 10060)


def next_support_id():
    """受付番号。同じ秒に2件出ても重ならないよう、2件目からは連番を足す(-2, -3 ...)"""
    base = time.strftime("%m%d-%H%M%S")
    with _SID_LOCK:
        if base == _SID_LAST["base"]:
            _SID_LAST["n"] += 1
            return f"{base}-{_SID_LAST['n']}"
        _SID_LAST.update(base=base, n=1)
        return base


def cap_log(path, max_bytes=200_000, keep_bytes=50_000):
    """記録ファイルの上限。大きくなりすぎたら、末尾の keep_bytes だけ残す(いつも追記され続けるため)。
    ほかの処理が開いていて置き換えられなければ、何もしない(次の起動でもう一度見る)。戻り: 切り詰めたか"""
    try:
        p = Path(path)
        if not p.exists() or p.stat().st_size <= max_bytes:
            return False
        tail = p.read_bytes()[-keep_bytes:]
        nl = tail.find(b"\n")
        tail = "(古い部分は上限のため消しました)\n".encode("utf-8") + (tail[nl + 1:] if nl >= 0 else tail)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(tail)
        try:
            os.replace(tmp, p)
        except OSError:
            _rm(tmp)
            return False
        return True
    except OSError:
        return False


def support_error(where):
    """想定外の失敗。原文(例外の経過)を 出力/サポート記録.txt に受付番号つきで残し、画面には受付番号と次にすることだけを返す(D-11・E-22)。
    接続が途中で切れただけ(ブラウザを閉じた等)は記録しない"""
    sid = next_support_id()
    try:
        if not is_disconnect(sys.exc_info()[1]):
            OUT.mkdir(exist_ok=True)
            cap_log(OUT / SUPPORT_NAME, 300_000, 100_000)
            with open(OUT / SUPPORT_NAME, "a", encoding="utf-8") as f:
                f.write(f"===== 受付番号 {sid} {time.strftime('%Y-%m-%d %H:%M:%S')} {where}\n{traceback.format_exc()}\n")
    except OSError:
        pass
    return {"error": f"ツールの側で問題が起きました(受付番号 {sid})。少し待って、もう一度押します。直らないときは、この表示と受付番号を担当者に送ってください。"
                     "続くときは、この画面を写して連絡します", "support_id": sid}


def last_support_id():
    try:
        ids = re.findall(r"===== 受付番号 (\S+)", (OUT / SUPPORT_NAME).read_text(encoding="utf-8", errors="replace")[-20000:])
        return ids[-1] if ids else ""
    except OSError:
        return ""


def report_text(subject, symptom):
    """担当者へそのまま送れる文の部品(拒否の帯のコピーと、サポート用のコピーが同じ部品を使う)。版・受付番号・症状・時刻だけ。許可キーは入れない"""
    sid = last_support_id()
    return f"AIまるなげ営業の{subject}について。版 {VERSION}。{symptom}" + (f"最近の受付番号 {sid}。" if sid else "") + f"確認した時刻 {time.strftime('%Y-%m-%d %H:%M')}。"


def support_report(symptom=""):
    """サポート用の「担当者に送る文」。症状は画面の状態から作る(送信の途中・止まった・拒否など)"""
    parts = []
    if symptom.strip():
        parts.append("症状: " + symptom.strip()[:200] + "。")
    if JOB.get("running"):
        parts.append(f"いま「{JOB.get('name', '')}」が動いています。")
    elif JOB.get("name") == "本番送信" and JOB.get("code") is not None and not SEND_INFO.get("dismissed"):
        c = send_counts(SEND_INFO.get("started"))
        parts.append(f"直近の送信は全{SEND_INFO.get('total', 0)}社のうち{sum(c.values())}社を終えて止まっています(終了コード{JOB.get('code')})。")
    try:
        lic = license_check.status() or {}
        if lic.get("state") not in (None, "", "許可"):
            parts.append(f"利用の確認: {lic.get('state')}（{lic.get('reason') or 'なし'}）。")
    except Exception:
        pass
    try:
        eng = ai_engine.engine()
        parts.append("使う AI: " + ("Muse" if eng == "muse" else "Claude Code") + (f"（接続: {muse_link.status()['state']}）" if eng == "muse" else "") + "。")
    except Exception:
        pass
    return report_text("困りごと", "".join(parts) or "症状は画面で確かめます。")


def ai_engine_view():
    """画面の「使う AI を選ぶ」が読む状態: 選択(読めなければ claude_code と理由)と Muse との接続状態。キーの値は含めない"""
    return dict(ai_engine.load(), muse=muse_link.status(), link_here=muse_link_owner())


def muse_link_owner():
    """Muse との接続を持つのは入口の番号(と開発用)の画面サーバーだけ(同じ導入から接続を何本も張らない)"""
    return PORT == ports.MAIN or IS_DEV


def muse_link_apply():
    """AI設定の保存の直後と起動時に呼ぶ。muse なら接続を始め(つなぎ直し)、claude_code なら閉じる。claude_code の間は外向きの通信をしない"""
    if not muse_link_owner():
        return None
    return muse_link.apply(PORT, TOKEN, VERSION, proposal_links.read_key, license_check.install_id)


def bug_cfg():
    """不具合の提出(bug_report.py)の設定。許可キー・導入ID・版は利用許可の照合と同じものが自動で付く(利用者は入力しない)"""
    url = license_check._url().rsplit("/", 1)[0] + "/report.php"
    return bug_report.Config(license_check.SERVICE_ID, license_check.PRODUCT_VERSION, url, proposal_links.read_key, license_check.install_id)


def license_report(st):
    """利用許可の状態に、担当者へそのまま送れる文(report)を足す。鍵の値は入れない(状態・理由・版・受付番号だけ)"""
    st = dict(st)
    st["key_set"] = bool(proposal_links.read_key())  # 画面の「許可キー: 設定済み」用。有無だけで、値は返さない(REQ-1001)
    st["report"] = report_text("利用の確認", f"状態 {st.get('state', '')}（理由 {st.get('reason') or 'なし'}）。画面の文: {st.get('message') or 'なし'}。")
    return st


def restore_interrupted():
    """前回の送信が途中で終わった(ツールごと落ちた等)なら、起動時に「N社残っています」を出せるよう、送信の状態を戻す。
    記録(送信中_記録_<番号>.json)は本番の開始時に書き、生きて終えたときに消す。残っていれば、終わりの処理が走らなかった。
    別の番号のサーバーが生きている間・記録を書いたプロセスか送信の子が動いている間は、その記録に触れない。読めない記録は壊れた名前に退避して無視する"""
    if JOB["running"]:
        return False
    dead = []
    for f in sorted(OUT.glob(SEND_MARK_GLOB)):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except OSError:
            continue
        except ValueError:
            try:
                f.rename(f.with_name(f.name + ".壊れ.bak"))  # 壊れた記録が残り続けないように、別名にして残す
            except OSError:
                pass
            continue
        if not isinstance(d, dict):
            continue
        if d.get("port") and d.get("port") != PORT and ports.job(int(d["port"]), timeout=5) is not None:
            continue  # 記録を書いた番号のサーバーが、まだ生きている(送信の最中かもしれない)。その記録は、そのサーバーのもの
        if d.get("pid") != os.getpid() and (_pid_alive(d.get("pid")) or _pid_alive(d.get("child"))):
            continue  # 書いたプロセス、または送信の子が、まだ動いている。送り直しを誘う表示を出さない
        dead.append((f, d))
    if not dead:
        return False
    d = dead[0][1]
    SEND_INFO.update(total=int(d.get("total") or 0), started=str(d.get("started") or ""), dismissed=False, interrupted=True)
    JOB.update(name="本番送信", code=-1, running=False, lines=[], fresh=False)
    for f, _ in dead:
        _rm(f)
    return True


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def key_parts(p, mx, supp):
    """確認のあとで何が変わったかを言うための材料(確認番号そのものは key の方で照合する。これは文を作るためだけ)"""
    msg = b"".join(f.name.encode("utf-8") + b"\0" + f.read_bytes() for f in sorted(MSG.glob("*.txt"))) if MSG.exists() else b""
    import patterns
    msg += b"".join(f.name.encode("utf-8") + b"\0" + f.read_bytes() for f in sorted(patterns.PATTERN_DIR.glob("*.txt")))
    return {"リスト": [str(p), p.stat().st_mtime_ns], "送る会社の数": mx, "検索で見つけたホームページの会社にも送るか": bool(supp),
            "送信元": _sha(SENDER.read_bytes()) if SENDER.exists() else "", "文面": _sha(msg)}


CHANGE_WORDS = {"リスト": "送るリストか書き出し", "送る会社の数": "1回に送る社数", "検索で見つけたホームページの会社にも送るか": "くわしい条件",
                "送信元": "送信元", "文面": "文面"}


def changed_text(parts):
    """E-15。確認のあとで変わったものの名前を言う。材料が同じなら、送らない会社か送信の記録が変わった"""
    old = PREVIEW.get("parts") or {}
    names = [CHANGE_WORDS[k] for k in CHANGE_WORDS if k in old and old[k] != parts.get(k)]
    what = "・".join(names) if names else "送らない会社か送信の記録"
    return f"送る内容を出したあとで、{what}が変わりました。先にもう一度「送る内容を出す」を押します"


def fill_drafts(p, supp, store=None):
    """D-2。送る会社のうち書き出しが空の会社に、下書きをリストへ書き込む(作り方の列も)。書いた社数を返す。
    書いてから確認番号を作るので、確認と本番で同じ全文になる。書き出しのある会社(人・AIが書いたもの)は変えない"""
    import autosend
    want = {(t[0], t[1]) for t in autosend.targets(str(p), supp, gate=store)} if store is not None else {(t[0], t[1]) for t in autosend.targets(str(p), supp)}
    uses_opener = {}  # 業種ごとに、使う文面に会社ごとの書き出しがあるか(無い文面の会社には書かない。書いても届く文に入らない)

    def needs(ind):
        if ind not in uses_opener:
            uses_opener[ind] = "{書き出し}" in autosend.pick_message_source(str(MSG), ind)[1]
        return uses_opener[ind]
    with filelock.locked(p):
        rows = read_csv(p)
        n = 0
        for r in rows:
            if (r.get("企業名", ""), r.get("問い合わせフォームURL 無ければメール", "")) in want and not (r.get("書き出し") or "").strip() and needs(r.get("業種", "")):
                d, src = opener_draft_for(industry=r.get("業種", ""), basis=r.get(BASIS_COL, ""), taken={(x.get("書き出し") or "").strip() for x in rows})
                if not d:
                    continue  # 送る文面に提案語が無く自動の下書きも作れない会社だけが空のまま
                r["書き出し"], r[OPENER_COL] = d, OPENER_SOURCES[src]
                n += 1
        if n:
            _write_rows(p, rows)
    return n


def _write_rows(p, rows):
    fields = list(rows[0].keys()) if rows else []
    for c in ("書き出し", OPENER_COL, BASIS_COL, BASIS_URL_COL, BASIS_WHY_COL):
        if c not in fields:
            fields.append(c)
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\r\n", restval="")
    w.writeheader()
    w.writerows(rows)
    filelock.atomic_write(p, buf.getvalue())


def job_view():
    """画面に見せる処理(動いている方。両方動いていれば送る系)と、もう一方。どちらも止まっていれば、あとに始めた方"""
    if JOB["running"] or JOB.get("fresh"):  # 送る系が終わったのに、画面へまだ返していない間は、探すが動いていても送る系を主にして返す
        return JOB, SJOB
    if SJOB["running"]:
        return SJOB, JOB
    return (JOB if JOB.get("started", "") >= SJOB.get("started", "") else SJOB), None


def busy_error():
    """E-17。ほかの処理が動いているとき、何が動いているかを言う"""
    if not JOB["running"]:
        return None
    what = {"探す": "「探す」", "確認": "「送る内容を出す」", "本番送信": "送信", "手動入力": "「フォームで送る(自動挿入)」の入力"}.get(JOB["name"], "ほかの処理")
    return {"error": f"いま{what}が動いています。終わってから押します", "started": False}


def send_counts(since, running=False):
    """送信ログのうち since(本番を始めた時刻)以降の行を、送れた・送れたか確かめる・フォームで送る(自動挿入) に数える。
    1社につき数えるのは最後の1行だけ(押す直前に「送信試行」を書き、そのあと「送信済み」を書くので、全部の行を数えると送れた会社が「確かめる」にも入る。
    陽性対照: test_send_counts_count_each_company_once_by_its_last_row)"""
    out = {"sent": 0, "check": 0, "manual": 0}
    if not since or not _log().exists():
        return out
    last = {}
    for r in read_csv(_log()):
        if (r.get("日時") or "")[:16] < since[:16]:
            continue
        last[(r.get("企業名") or "", r.get("フォームURL") or "")] = r
    for r in last.values():
        res = r.get("結果") or ""
        if running and res == "送信試行":  # 送っている最中の1社。まだ「終わった」には数えない(終わってから残っていれば、確かめる会社に入る)
            continue
        if res == "送信済み":
            out["sent"] += 1
        elif res in sales_records.UNSURE:
            out["check"] += 1
        elif res == "要手動":
            out["manual"] += 1
    return out


def send_state(q, store=None):
    """送る画面の状態(30_構成 5-2 の s0〜s7)を1つだけ決める。画面はこれを描くだけで、状態を推し量らない。
    返す形: {"send_state", "problems", "counts", "changed", "file"}。重い判定(送る会社と全文を作る)は、確認の印があるときだけ行う"""
    out = {"problems": [], "counts": {}, "changed": "", "file": ""}
    if JOB["running"] and JOB["name"] == "本番送信":
        c = send_counts(SEND_INFO.get("started"), running=True)
        out["counts"] = {"total": SEND_INFO.get("total", 0), "done": sum(c.values()), **c}
        return dict(out, send_state="s6", awake=JOB.get("awake", ""), stopping=send_stop_flag().exists())
    if JOB["name"] == "本番送信" and JOB.get("code") is not None and not SEND_INFO.get("dismissed"):
        c = send_counts(SEND_INFO.get("started"))
        tot = SEND_INFO.get("total", 0)
        done = sum(c.values())
        # 全社数と終えた数を比べる。終わった状態(コード0)でも、止めた・落ちたなら「送り終えました」とは言わない(S3)
        out["counts"] = {"total": tot, "done": done, "remaining": max(tot - done, 0), "interrupted": bool(SEND_INFO.get("interrupted")),
                         "stopped": any("止める指示があったため" in str(l) for l in JOB.get("lines", [])), **c}
        return dict(out, send_state="s7")
    files = outputs()
    name = str(q.get("file") or "") or (files[0] if files else "")
    p = safe_out(name) if name else None
    if not p or not str(p).endswith(".csv"):
        return dict(out, send_state="s0")
    out["file"] = p.name
    try:
        probs = gate_problems(read_sender(), templates_for(p))
    except PermissionError:
        return dict(out, send_state="s3", problems=[{"field": LIST_FIELD, "kind": "Excel", "text": excel_error(p)["error"]}])
    out["problems"] = probs
    if any(x["kind"] in ("未保存", "空") for x in probs):
        return dict(out, send_state="s1")
    if probs:
        return dict(out, send_state="s2")
    if PREVIEW["key"] is None or JOB["running"]:
        return dict(out, send_state="s3")
    try:
        mx = count(q.get("max"), None, 1, None) if "max" in q else PREVIEW["key"][2]  # 空欄=未指定=全社。max が来ていないときは出した送る内容の数
    except ValueError:
        mx = PREVIEW["key"][2]
    supp = q.get("supp") == "1" if "supp" in q else PREVIEW["key"][3]
    pl = plan(p, supp, mx, store, explain=True)
    key = (str(p), p.stat().st_mtime_ns, mx, supp, fingerprint(), pl["digest"])
    if key != PREVIEW["key"]:
        return dict(out, send_state="s3", changed=changed_text(key_parts(p, mx, supp)))
    out["counts"] = {"sendable": len(pl["todo"]), "skipped": len(pl["skipped"])}
    return dict(out, send_state="s5" if pl["skipped"] else "s4")


def count(v, default, lo, hi):
    """空なら既定値(None もありうる)。0 や範囲外を既定値に化けさせず、ValueError にする。hi が None なら上限なし"""
    n = default if v in (None, "") else int(float(v))
    if n is None:
        return None
    if n < lo or (hi is not None and n > hi):
        raise ValueError(n)
    return n


def safe_out(name):
    if not re.fullmatch(r"フォームリスト_[^\\/]+\.(csv|xlsx)|送信候補_[^\\/]+\.csv", name or ""):
        return None
    p = (OUT / name).resolve()
    return p if p.parent == OUT.resolve() and p.exists() else None


def _send_notice(body):
    """送信は止まらないが、先方に届く文面として足りない点(特定電子メール法の配信停止の案内など)"""
    return "" if re.search(r"不要|配信停止|停止|お断り", body) else "配信停止の案内(「不要ならご返信ください」など)が文面に見当たりません。送ることはできますが、署名に足すことをお勧めします"


def _send_warn(body):
    """本番の送信と同じ判定(autosend.pyの送信ループ)を、確認の画面でも先に見せる。送れない文面を『確認済み』と思わせない"""
    import placeholders  # 本番の送信ループ・build_plan と同じ判定(見出し1行の【…】は止めない)
    left = placeholders.leftovers(body)
    if left:
        w = left[0]
        what = f"見本の「{w['word']}」" if w["kind"] == "見本" else (f"置き換わらない「{w['word']}」の部分" if w["word"] else "閉じていない括弧")
        return f"文面に{what}が残っています。このままでは送られません"
    if not re.search(r"@|\d{2,4}-\d{2,4}-\d{3,4}", body):
        return "送信者の連絡先(メールか電話)が文面に入っていません。このままでは送られません"
    return ""


class Srv(ThreadingHTTPServer):
    """同じポートに2つ目を立てず、ほかのプロセスに乗っ取られないようにする念のための設定。Python 3.14.3 の Windows では、設定しなくても2つ目は立たなかった(2026-10-07 実測)"""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else (json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def guard(self, api):
        """他サイトの画面からの操作を断る: Hostが自分、Originがあれば自分、APIはトークン必須"""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if host not in HOSTS:
            self.send(403, {"error": f"アクセス先の名前が違います（{host}）。http://127.0.0.1:{PORT}/ で開いてください", "code": "host"})
            return False
        if origin and origin.replace("http://", "") not in HOSTS:
            self.send(403, {"error": f"別のサイトからの操作です（{origin}）", "code": "origin"})
            return False
        if api and self.headers.get("X-Token") != TOKEN:
            self.send(403, {"error": "この画面は古くなっています（ツールを起動し直したため）。読み込み直します", "code": "token"})
            return False
        return True

    def from_browser_screen(self):
        """Origin は誰でも付けられる。画面(ブラウザ)を開いたときにだけ渡す HttpOnly の cookie も合うときだけ、画面からの操作とみなす"""
        origin = (self.headers.get("Origin") or "").replace("http://", "")
        if origin not in HOSTS:
            return False
        got = ""
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == UI_COOKIE:
                got = v
        return bool(got) and secrets.compare_digest(got, UI_SESSION)

    def manual_kit(self, q, ng_store):
        """自動で送れなかった会社(要手動・手で確かめる)に、会社ごとに置き換えた件名と全文を出す。送るのは本人の手"""
        p, err = list_or_error(q.get("file", ""))
        if err:
            return self.send(*err)
        sender = read_sender()
        probs = send_rules.sender_problems(sender)
        if probs:
            return self.send(400, problem_error(probs))
        import autosend
        manual ={url for (_n, url), r in sales_records.final_states(LOG).items() if r.get("結果") == "要手動"}
        rows = []
        for r in read_csv(p):
            url = r.get("問い合わせフォームURL 無ければメール", "")
            if not url.startswith("http") or not (url in manual or r.get("フォーム判定") == "要目視"):
                continue
            if r.get("営業お断りの表示") == "あり" or (ng_row_hidden(r, ng_store) if ng_store is not None else ng_list.is_ng_row(r, ng_list.load())):
                continue
            if send_rules.company_refusal(LOG, r.get("企業名", ""), url, r.get("HP URL", "")):
                continue
            rows.append((r.get("企業名", ""), url, r.get("業種", ""), r.get("書き出し", ""), r.get("HP URL", "")))
        try:
            todo, bodies, rejected = autosend.prepare_message_plan(rows[:50], MSG, sender)
        except RuntimeError as e:
            return self.send(400, {"error": str(e)})
        out = [{"company": t[0], "form": t[1], "subject": autosend.subject_for(t[0], t[1], sender, autosend.subject_industry(MSG, t[2])), "message": b}
               for t, b in zip(todo, bodies)]
        return self.send(200, {"companies": out, "skipped": [{"company": n, "why": w} for n, _u, w in rejected],
                               "legal_missing": send_rules.legal_missing(sender), "window": send_rules.window_refusal()})

    def targets(self, q, ng_store=None):
        """送る内容(表示用)。送る会社と全文・送らない会社と理由・文面ごとの共通の文。数え方は確認・本番と同じ build_plan(1つの関数)。
        送ってよいかはここでは決めない(本番は「送る内容を出す」の印と人の承認が要る)。key_short は、出した送る内容がいまも有効なときだけ入る"""
        import autosend
        p, err = list_or_error(q.get("file", ""))
        if err:
            return self.send(*err)
        sender = read_sender()
        probs = send_rules.sender_problems(sender)
        if probs:
            return self.send(400, problem_error(probs))
        try:
            mx = count(q.get("max"), None, 1, None)
        except ValueError:
            return self.send(400, count_error())
        supp = q.get("supp") == "1"
        try:
            pl = plan(p, supp, mx, ng_store, explain=True)
            src = {(r.get("企業名", ""), r.get("問い合わせフォームURL 無ければメール", "")): r for r in read_csv(p)}
            rows, commons = [], {}
            for (n, u, ind, op, hp), body, subject, label in zip(pl["todo"], pl["bodies"], pl["subjects"], pl["labels"]):
                rows.append({"name": n, "form": u, "industry": ind, "hp": hp, "template_label": label, "opener": op,
                             "opener_source": opener_source(src.get((n, u), {})), "opener_why": opener_why(src.get((n, u), {})), "subject": subject,
                             "warn": _send_warn(body), "notice": _send_notice(body), "text": body,
                             "adoption_diff": autosend.message_adoption.difference_text(autosend.message_adoption.changed_parts(n, u, body, sender))})
                _, template, local_sender = autosend.message_adoption.resolve_row((n, u, ind, op, hp), str(MSG), sender, autosend.pick_message)
                c = commons.setdefault((label, template), {"template_label": label, "subject": marked_subject(local_sender),
                                                            "text_with_marks": marked_text(template, local_sender), "count": 0})
                c["count"] += 1
        except PermissionError:
            return self.send(409, excel_error(p))
        except RuntimeError as e:  # 計測用の鍵が無い等、原因が分かっているもの(利用者向けの文)
            return self.send(400, {"error": str(e)})
        except Exception:
            return self.send(400, support_error("targets"))
        skipped = [dict({"name": s["name"], "url": s.get("url", ""), "reason_code": s["reason_code"], "text": s["text"], "fix": s["fix"]},
                        **({"industry": src.get((s["name"], s.get("url", "")), {}).get("業種", "") or "default"} if s["fix"] == "message" else {}))
                   for s in pl["skipped"]]
        valid = PREVIEW["key"] is not None and pl["digest"] and PREVIEW["key"] == (str(p), p.stat().st_mtime_ns, mx, supp, fingerprint(), pl["digest"])
        return self.send(200, {"sendable": rows, "skipped": skipped, "commons": list(commons.values()), "marks": list(MARK_WORDS),
                               "key_short": short_key(pl["digest"]) if valid else None, "key": pl["digest"] if valid else None, "window": send_rules.window_refusal(),
                               "over_limit": pl["over_limit"], "stop": pl["stop"]})

    def send_api(self, d, mx, ng_store):
        """送る内容を出す(preview)と本番(send)。練習(試走)の道は無い(D-5)。
        止める順: リスト(E-1〜E-3) → 送信元と文面(欄の名前つき。処理を始める前に止める) → ほかの処理(E-17) → 送らない会社の記録(E-21) → 確認番号"""
        real, preview = bool(d.get("send")), bool(d.get("preview"))
        if not real and not preview:  # 古いMCP(formlist_practice)・古い画面から来ても、練習は走らせない
            return self.send(400, {"error": "練習はなくなりました。「送る内容を出す」を使ってください", "code": "practice_removed"})
        p, err = list_or_error(d.get("file", ""))
        if err:
            return self.send(*err)
        try:
            probs = gate_problems(read_sender(), templates_for(p))
        except PermissionError:
            return self.send(409, excel_error(p))
        if probs:
            return self.send(400, problem_error(probs))
        busy = busy_error()
        if busy:
            return self.send(409, busy)
        supp = bool(d.get("supp"))
        args = ["autosend.py", str(p), "--sender", str(SENDER), "--message", str(MSG)]
        if mx is not None:  # 未指定なら --max を付けない(対象の全社)
            args += ["--max", str(mx)]
        if ng_store is not None:
            args += ["--ng-state-db", str(ng_store.db_path)]
        if supp:
            args.append("--include-supplemented")
        sync_ng_txt(ng_store)
        if real and (ng_store is None or NG_MIGRATION["error"] or (_NG_SYNC and ng_store.migration_status() != "canonical")):  # E-21(本番を止める3つ。原文はサポート記録へ)
            sid = None
            if NG_MIGRATION["error"]:
                try:
                    raise RuntimeError(NG_MIGRATION["error"])
                except RuntimeError:
                    sid = support_error("ng_migration")["support_id"]
            return self.send(409, {"error": "送らない会社の一覧を読み込めなかったので、本番は送れません。ツールを終了して起動し直します", "support_id": sid})
        try:
            if preview:
                fill_drafts(p, supp, ng_store)  # D-2: 書き出しが空の送る会社に下書きを書き込んでから番号を作る(確認と本番で同じ全文)
            key = (str(p), p.stat().st_mtime_ns, mx, supp, fingerprint(), digest(p, supp, mx, ng_store))
            parts = key_parts(p, mx, supp)
        except PermissionError:
            return self.send(409, excel_error(p))
        except Exception:
            return self.send(400, support_error("send"))
        if preview:
            save_settings(last_max=mx)  # D-9: 前回の最大の社数(読み込み直し・起動し直しでも戻る)
            if key[-1] is None:  # 送れる会社が0。理由は /api/targets の skipped に出る
                return self.send(409, {"error": "送れる会社がありません。理由は「送らない会社」の一覧に出ています", "code": "nothing_to_send"})
            skipped_n = len(LAST_PLAN.get("skipped") or []) if LAST_PLAN.get("digest") == key[-1] else 0

            def ok(k=key, pt=parts, n=skipped_n):  # 確認の出力に、印と同じ確認番号が出たときだけ印を立てる(見せた全文と印を一致させる)
                if f"確認番号: {k[-1]}" in JOB["lines"]:
                    set_preview(k, pt)
                    PREVIEW["skipped"] = n
            started = run_job("確認", args + ["--preview"], on_ok=ok)
            return self.send(200, {"started": True} if started else busy_error() or {"started": False})
        if d.get("confirm") != "送信":
            return self.send(400, {"error": "「届く文と、会社ごとに違うところを読みました」にチェックを入れてから送ります(AIから送るときは confirm に「送信」と入れます)"})
        if PREVIEW["key"] is None:
            return self.send(400, {"error": "まだ「送る内容を出す」を押していません。先に押して、届く文を読みます"})
        if PREVIEW["key"] != key:
            return self.send(400, {"error": changed_text(parts)})  # E-15。変わったものの名前を言う
        if APPROVED["key"] != key:
            return self.send(400, {"error": "人の承認がありません。画面で届く文を読み、送るボタンを押して、画面の外に出る確認で「はい」を押します(AIは承認を押せません)"})
        total = len(PREVIEW.get("names") or [])
        PREVIEW.update(key=None, names=[], parts={})  # 確認は1回の本番ごとに使い切る
        APPROVED["key"] = None
        args += ["--send", "--expect", key[-1]]  # 送る本人が、確認時と同じ会社・全文・送信元かを照合し、違えば送らずに止まる
        token = approval.issue_ticket(key[-1])  # 承認のあとにだけ発行する使い切りの鍵。autosend --send はこれが無いと始まらない
        SEND_INFO.update(total=total, started=time.strftime("%Y-%m-%d %H:%M"), dismissed=False, interrupted=False)
        started = run_job("本番送信", args, "送信\n", extra_env={approval.TICKET_ENV: token, approval.PORT_ENV: str(PORT), approval.UI_TOKEN_ENV: TOKEN})
        if not started:
            approval.revoke_ticket()
            SEND_INFO["dismissed"] = True
            return self.send(409, busy_error() or {"error": "いまほかの処理が動いています。終わってから押します", "started": False})
        return self.send(200, {"started": True})

    def render_sample(self, d):
        """文面の見本(D-10・M3)。送るリストの1社目(無ければ「株式会社サンプル」)で、autosend.render と同じ置き換えをした件名と全文。
        d: {industry, template_parts:{greeting, opener, body, signature}(省略時は保存済みの文面), file(省略時は最新のリスト)}"""
        import autosend
        import placeholders
        sender = read_sender() or {}
        industry = str(d.get("industry") or "")
        parts = d.get("template_parts")
        if isinstance(parts, dict) and isinstance(parts.get("text"), str) and parts["text"].strip():
            template = parts["text"]
        elif isinstance(parts, dict):
            template = join_message(parts)
        else:
            template = autosend.pick_message_source(str(MSG), industry)[1] if MSG.exists() else ""
        name, url, opener, label = "株式会社サンプル", "", "", "見本の会社(株式会社サンプル)。送るリストがまだ無いので、この名前で作りました"
        files = outputs()
        f = safe_out(str(d.get("file") or "")) or (safe_out(files[0]) if files else None)
        if f and str(f).endswith(".csv"):
            try:
                rows = [r for r in read_csv(f) if r.get("企業名")]
            except PermissionError:
                return self.send(409, excel_error(f))
            cand = sample_rows_for(rows, industry)
            if not cand and industry and industry != "default":
                label = f"見本の会社(株式会社サンプル)。送るリストに{industry}の会社が無いので、この名前で作りました"
            rows = cand
            if rows:
                # 「送らない」を選んだ会社・送れない会社は見本に使わない。確認・本番と同じ数え方(plan)で、実際に送る会社の中から選ぶ
                try:
                    todo = plan(f, False, None, _NG_STORE, explain=True)["todo"]
                except Exception:
                    todo = None
                if todo is None:  # 数えられないときは、送らないリストに載る会社だけは避けて、先頭の会社を使う
                    r = rows[0]
                else:
                    sendable = {(n, u) for (n, u, _i, _o, _h) in todo}
                    r = next((x for x in rows if (x["企業名"], x.get("問い合わせフォームURL 無ければメール", "")) in sendable), None)
                if r is not None:
                    name, url = r["企業名"], r.get("問い合わせフォームURL 無ければメール", "")
                    opener = (r.get("書き出し") or "").strip() or opener_draft_for(industry=r.get("業種", ""), basis=r.get(BASIS_COL, ""))[0]
                    label = f"実際に送る会社の1社目({name}、業種: {r.get('業種', '')})で作りました"
                else:
                    label = "見本の会社(株式会社サンプル)。送る会社が無いので、この名前で作りました"
        if not opener:
            opener = "《この会社の書き出しはまだありません。ホームページから方針・思いが拾えた会社には、その引用が入ります》"  # 見本だけの印。送る文には入らない(空のままでは送らない)
        try:
            text = autosend.render(template, name, opener, sender)
        except Exception:
            return self.send(400, support_error("render_sample"))
        left = placeholders.leftovers(text)
        for w in left:  # 置き換わらない部分は記号のまま見せず、印にする(何が足りないかは problems で言う)
            if w["match"] == "{資料URL}":
                text = text.replace(w["match"], "《資料のリンク(送信元の資料URLが空です)》")
        probs = message_problems({industry or "標準": template})
        if any(w["match"] == "{資料URL}" for w in left):
            probs.append({"field": "資料URL", "kind": "資料URLが空", "text": "文面に資料のリンクがありますが、送信元の「資料URL」が空です"})
        return self.send(200, {"subject": autosend.subject_for(name, url, sender, autosend.subject_industry(MSG, industry)), "text": text, "company_label": label, "problems": probs})

    def do_GET(self):
        try:
            return self._get()
        except AssertionError:  # 試験の見張り(書いてはいけない場所への書き込み等)は握りつぶさない
            raise
        except Exception:  # 想定外の失敗。原文はサポート記録へ、画面には受付番号だけ(E-22。応答に例外の文字を出さない)
            return self.send(500, support_error("GET " + urlparse(self.path).path))

    def do_POST(self):
        try:
            return self._post()
        except AssertionError:
            raise
        except Exception:
            return self.send(500, support_error("POST " + urlparse(self.path).path))

    def _get(self):
        u = urlparse(self.path)
        # /api/whoami だけは、トークン無しで答える(MCP・導入・ショートカットが「この番号で動いているのは自分のツールか」を確かめる入口)。
        # 返すのは、フォルダ・版・番号・動作中かだけ。会社名・文面・ログは返さない。Host/Origin の検査は他と同じ
        if not self.guard((u.path.startswith("/api/") and u.path != "/api/whoami") or u.path == "/download"):
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        ng_store = _NG_STORE
        if u.path == "/":
            # 別枠の画面(ボタン・自動振り分けで開いたもの=URLに peer= が付く)と、探す・送るの最中の画面は、入口へ戻さない(戻すと別枠の検索が始められず、動いている検索の画面も消える)
            if not IS_DEV and PORT != ports.MAIN and "peer" not in q and not (JOB["running"] or SJOB["running"]) and self.headers.get("Sec-Fetch-Mode") == "navigate" and ports.job(ports.MAIN, timeout=1):  # 開発用(8785)はお客様版の入口へ案内しない
                # 画面のURLは常に入口の番号(8780)に固定する。8781・8782 をブックマークしていても、動いている入口へ案内する(古い画面のまま使わせない)
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{ports.MAIN}/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            extra = {}
            if self.headers.get("Sec-Fetch-Mode") == "navigate" and self.headers.get("Sec-Fetch-Dest") == "document":  # ブラウザが画面として開いたときだけ。APIの呼び出しには渡さない
                extra["Set-Cookie"] = f"{UI_COOKIE}={UI_SESSION}; HttpOnly; SameSite=Strict; Path=/"
            return self.send(200, page().replace("__TOKEN__", TOKEN), "text/html; charset=utf-8", extra)
        if u.path == "/api/meta":
            # 送る画面の状態はここで1つに決める(画面は推し量らない)。q: file(送るリスト。無ければ最新) max supp(出した送る内容と比べるとき)
            sender = read_sender()
            msgs = messages()
            info = message_info()
            first = first_time()
            last_max, max_kind = default_max(first)
            st = send_state(q, ng_store)
            return self.send(200, {"industries": list(osm_source.INDUSTRY_TAGS), "sender": sender or {}, "files": outputs(), "messages": msgs,
                                   "message_parts": {k: split_message(v) for k, v in msgs.items()}, "message_info": info,
                                   "send_state": st["send_state"], "problems": st["problems"], "counts": st["counts"], "changed": st["changed"],
                                   "send_file": st["file"], "awake": st.get("awake", ""), "stopping": st.get("stopping", False), "first_time": first, "last_max": last_max, "max_kind": max_kind, "limits": limits(),
                                   "message_summary": {"default": 1, "industries": [n for n, v in info["uses"].items() if v == "業種ごと"]}, "copy": copy_info()})
        if u.path == "/api/message/patterns":
            # 業種の型(パターン)ごとの文面テンプレートの状態と中身。文面が読めない(欠けている)番号は patterns_missing に出す
            import patterns
            status = patterns.pattern_status()
            return self.send(200, {"pattern_status": {n: dict(s, text=patterns.load_pattern(n)) for n, s in status.items()}, "patterns_missing": patterns.patterns_missing()})
        if u.path == "/api/settings/history":
            # 保存のたびに残る、変わる前の送信元・文面の控え。引数なし=一覧(中身は返さない)。version と file=その1ファイルの中身(計測キーは sender.json に入らない設計)
            if q.get("version") and q.get("file"):
                text = settings_guard.history_item(SENDER.parent, q["version"], q["file"])
                if text is None:
                    return self.send(404, {"error": "その版・そのファイルはありません"})
                return self.send(200, {"version": q["version"], "file": q["file"], "text": text})
            return self.send(200, {"versions": settings_guard.history(SENDER.parent)})
        if u.path == "/api/handoff":
            # 別枠の画面が開いた直後に、元の画面から渡された検索条件を1回だけ受け取る
            d = handoff_take(q.get("id"))
            return self.send(200, {"req": d} if d else {"req": None})
        if u.path == "/api/whoami":
            return self.send(200, {"tool": "formlist", "here": str(HERE), "version": VERSION, "build": BUILD, "port": PORT, "dev": IS_DEV, "running": JOB["running"] or SJOB["running"], "name": JOB["name"] if JOB["running"] else SJOB["name"], "cond": SEARCH_COND.get("key", "") if SJOB["running"] else "", "watch": watch_status(),
                                   "asking": HUMAN["asking"] > 0, "last_action_age": (round(time.monotonic() - HUMAN["last_action"]) if HUMAN["last_action"] else None),
                                   "engine": ai_engine.engine(), "muse": {k: muse_link.status()[k] for k in ("state", "message", "last_ok_at")}})  # 使う AI と Muse の接続状態(キーの値は含めない)
        if u.path == "/api/ai_engine":  # 使う AI の設定(読み取り)。読めないときは claude_code と理由(note)を返す
            return self.send(200, ai_engine_view())
        if u.path == "/api/summary":
            # この画面(番号)の、最後の探索の内訳(段階別の件数・結果・理由)。画面の「結果を見る」が読む
            try:
                p = OUT / f"最後の探索_内訳_{PORT}.json"  # formlist.py は読み込みに十数秒かかるので、ここでは読み込まない(名前の決め方は formlist.summary_name と同じ)
                return self.send(200, json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {})
            except Exception:
                return self.send(200, {"error": "前回の探索の内訳を読めませんでした。リストはそのまま使えます", "support_id": support_error("summary")["support_id"]})
        if u.path == "/api/update":
            now = time.time()
            if not UPDATE_CACHE or now - UPDATE_CACHE["at"] > 300 or q.get("force"):
                UPDATE_CACHE.update(at=now, result=updater.check())
            busy = JOB["running"] or SJOB["running"] or any((ports.job(p) or {}).get("running") for p in ports.POOL if p != PORT)
            return self.send(200, {**UPDATE_CACHE["result"], "busy": bool(busy), "restart_needed": updater.build() != BUILD, "build": BUILD, "auto": updater.read_status()})
        if u.path == "/api/support_report":  # 困ったときに担当者へそのまま送れる文(版・受付番号・症状)。許可キーは入れない
            return self.send(200, {"report": support_report(str(q.get("symptom") or ""))})
        if u.path == "/api/bug_report/info":  # 不具合フォームを開いたとき: 版と、許可キーの有無だけ(鍵の値は返さない)
            return self.send(200, {"version": license_check.PRODUCT_VERSION, "key_set": bool(proposal_links.read_key())})
        if u.path == "/api/license":  # 利用許可の状態(通信はしない。手元の記録だけから答える)
            return self.send(200, license_report(license_check.status()))
        if u.path == "/api/job":
            v, o = job_view()
            if v is JOB and not JOB["running"]:
                JOB["fresh"] = False
            other = {"name": o["name"], "last": ([l for l in o["lines"] if l.strip()] or [""])[-1][:120]} if o is not None and o["running"] else None
            return self.send(200, {"lines": v["lines"][-300:], "running": v["running"], "name": v["name"], "code": v.get("code"), "files": outputs(), "version": VERSION, "build": BUILD, "here": str(HERE), "started": v.get("started", ""), "port": PORT,
                                   "other": other})
        if u.path == "/api/shot":
            # 1社目の確認で見せる、押す直前に保存した画面。出力/送信画面 の中の .png だけを返す(ほかの場所は読ませない)
            name = q.get("file") or q.get("name", "")
            shots = (OUT / "送信画面").resolve()
            p = (shots / name).resolve() if re.fullmatch(r"[^\\/:*?\"<>|]+\.png", name or "") else None
            if not p or p.parent != shots or not p.exists():
                return self.send(404, {"error": "保存した画面が見つかりません"})
            return self.send(200, p.read_bytes(), "image/png")
        if u.path == "/api/results":
            p = safe_out(q.get("file", ""))
            ng = None if ng_store is not None else ng_list.load()
            rows = [r for r in read_csv(p) if not (r.get("営業お断りの表示") == "あり" or (ng_row_hidden(r, ng_store) if ng is None else ng_list.is_ng_row(r, ng)))] if p else []
            return self.send(200, mail_annotate(rows))
        if u.path in MAIL_GET:
            return mail_get(self, u, ng_store)
        if u.path == "/api/ngscreen":
            return self.send(200, ng_screen(ng_store))
        if u.path == "/api/portals":
            return self.send(200, load_portals())
        if u.path == "/api/openings":
            f = safe_out(parse_qs(u.query).get("file", [""])[0])
            if not f or not str(f).endswith(".csv"):
                return self.send(404, {"error": "リストが見つかりません", "field": LIST_FIELD})
            try:
                rows = read_csv(f)
            except PermissionError:
                return self.send(409, excel_error(f))
            total = len(rows)
            # companies: 送る会社の全部(書き出しの有無に関係なく。保存しても欄が消えない)。送る会社の決め方は autosend.targets と同じ
            import autosend
            want = {(t[0], t[1]) for t in (autosend.targets(str(f), q.get("supp") == "1", gate=ng_store) if ng_store is not None else autosend.targets(str(f), q.get("supp") == "1"))}
            companies = [{"name": r.get("企業名", ""), "industry": r.get("業種", ""), "hp": r.get("HP URL", ""), "form": r.get("問い合わせフォームURL 無ければメール", ""),
                          "overview": r.get("HP概要", ""), "opener": (r.get("書き出し") or "").strip(), "opener_source": opener_source(r),
                          "basis": r.get(BASIS_COL, ""), "basis_url": r.get(BASIS_URL_COL, ""), "why": opener_why(r),
                          "draft": opener_draft_for(industry=r.get("業種", ""), basis=r.get(BASIS_COL, ""))[0]}
                         for r in rows if (r.get("企業名", ""), r.get("問い合わせフォームURL 無ければメール", "")) in want]
            if parse_qs(u.query).get("only_form", [""])[0] == "1":  # 画面用: 送る対象になりうる(フォーム検出・お断りなし)会社だけ
                rows = [r for r in rows if r.get("フォーム判定") == "フォームあり" and r.get("営業お断りの表示") != "あり"]
            return self.send(200, {"total": total, "companies": companies,
                                   "missing": [{"name": r.get("企業名", ""), "industry": r.get("業種", ""), "hp": r.get("HP URL", ""), "draft": opener_draft_for(industry=r.get("業種", ""), basis=r.get(BASIS_COL, ""))[0], "why": opener_why(r)} for r in rows if not (r.get("書き出し") or "").strip()][:200]})
        if u.path == "/api/targets":
            return self.targets(q, ng_store)
        if u.path == "/api/log":
            return self.send(200, read_csv(LOG) if LOG.exists() else [])
        if u.path == "/api/report":
            days = int(q["days"]) if str(q.get("days", "")).isdigit() and 1 <= int(q["days"]) <= 365 else 7
            return self.send(200, sales_records.report(LOG, days))
        if u.path == "/api/aftercare":  # 送信の記録の画面用: 最後の返事・返事に対応する会社・追いかける会社・「N社に送り、M社から返事」
            return self.send(200, sales_records.aftercare(LOG))
        if u.path == "/api/ngreplies":  # 返事で「不要」と言われて送らないリストに入れた会社(理由と日付つき)
            return self.send(200, ng_reply_rows(ng_store))
        if u.path == "/api/unsure":
            return self.send(200, sales_records.unsure(LOG))
        if u.path == "/api/opens":
            tpl = str((json.loads(SENDER.read_text(encoding="utf-8-sig")) if SENDER.exists() else {}).get("資料URL", ""))
            if proposal_links.MARK not in tpl:
                return self.send(200, {"companies": [], "total": 0, "note": "送信元の資料URLに {識別} が入っていないため、会社ごとの開封は数えていません"})
            try:
                return self.send(200, proposal_links.report(tpl, send_log=LOG))
            except RuntimeError as e:
                return self.send(502, {"error": str(e)})
        if u.path == "/api/manual_kit":
            return self.manual_kit(q, ng_store)
        if u.path == "/api/manual_draft":  # 「フォームで送る(自動挿入)」の前に、その会社に届く件名と本文(フォームに入る文)を見せる。通信しない
            p, err = list_or_error(q.get("file", ""))
            if err:
                return self.send(*err)
            probs = send_rules.sender_problems(read_sender())
            if probs:
                return self.send(400, problem_error(probs))
            import autosend
            name = str(q.get("name", "")).strip()
            row, bodies, problem = autosend.manual_precheck(str(p), str(SENDER), str(MSG), name)
            if problem:
                return self.send(400, {"error": f"理由: {problem[0]}。直し方: {problem[1]}"})
            sender = read_sender()
            return self.send(200, {"company": name, "subject": autosend.subject_for(row[0], row[1], sender, autosend.subject_industry(MSG, row[2])), "message": bodies[0],
                                   "adoption_diff": autosend.message_adoption.difference_text(autosend.message_adoption.changed_parts(row[0], row[1], bodies[0], sender))})
        if u.path == "/download":
            p = safe_out(q.get("file", ""))
            if not p:
                return self.send(404, "なし", "text/plain; charset=utf-8")
            return self.send(200, p.read_bytes(), "application/octet-stream",
                             {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(p.name)})
        self.send(404, "なし", "text/plain; charset=utf-8")

    def _post(self):
        if not self.guard(True):
            return
        if self.path != "/api/quit":  # 合言葉を通った画面・MCP からの書き込み(保存・開始・入力中の知らせ)の時刻。見張りは直近の操作中なら止めない(N3・R30)
            HUMAN["last_action"] = time.monotonic()  # 時計の巻き戻しに影響されない時計で測る(R28)
        if self.path == "/api/touch":  # 入力欄で入力している(保存前)ことの知らせ(R29)
            return self.send(200, {"ok": True})
        ng_store = _NG_STORE
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send(415, {"error": "json only"})
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            assert isinstance(d, dict)
            lim_in = count(d.get("limit"), None, 1, 1000)  # 出力する会社の数 = 送れる会社の目標数。空欄は None(下で検索する数の上限に合わせる。両方空欄なら10)
            mx = count(d.get("max"), None, 1, None)
            par = min(count(d.get("parallel"), 3, 1, 5), ports.PER_SERVER_MAX)  # 同時に動く検索の総量(サーバー数×1台あたり)は定数で決まる。画面・API から上げられない(4以上を指定されても3にする)
            flim_in = count(d.get("fetch_limit"), None, 0, 100000)  # 検索する数の上限 = 候補を集めて調べる総量の上限。0・空欄は未指定(出力する数に合わせる)
            import plan
            lim, flim, count_note = plan.normalize_counts(lim_in, flim_in)
        except Exception:
            if self.path in ("/api/send", "/api/targets"):  # 社数の欄の範囲外(E-12)。ほかの数の欄と分ける
                try:
                    count(d.get("max"), None, 1, None)
                except Exception:
                    return self.send(400, count_error())
            return self.send(400, {"error": "入力の形が正しくありません（数は1以上の整数で入れてください）"})
        if self.path in ("/api/message", "/api/sender", "/api/ng", "/api/ng/undo", "/api/openings") and JOB["running"] and JOB["name"] in ("本番送信", "確認"):
            if JOB["name"] == "確認":  # E-18
                return self.send(409, {"error": "送る内容を出している間は変えられません。数秒で終わります"})
            return self.send(409, {"error": "いま送っている最中は、送信元・文面・送らない会社・書き出しを変えられません。送り終わってから変えます"})
        if self.path == "/api/ai_engine":  # 使う AI の切り替え。送信元・文面・履歴には触れない。保存の直後に Muse との接続を始める/閉じる
            if not self.from_browser_screen():  # 承認と同じ: 人が開いた画面からだけ受ける(合言葉を持つ手元の AI・MCP からは切り替えられない)
                return self.send(403, {"error": "使う AI の切り替えは、ツールの画面（ブラウザ）で選んでください"})
            try:
                kept = ai_engine.save(d.get("engine"))
            except ValueError:
                return self.send(400, {"error": "使う AI は「Claude Code」か「Muse」から選びます"})
            muse_link_apply()
            return self.send(200, dict(ai_engine_view(), kept=bool(kept)))
        if self.path in ("/api/bug_report", "/api/bug_report/delete"):  # 不具合の提出と、本人の削除依頼(台帳サーバーへ。許可キー・版は自動で付く)
            try:
                if self.path == "/api/bug_report":
                    r = bug_report.submit(bug_cfg(), str(d.get("summary") or ""), str(d.get("detail") or ""), str(d.get("contact") or ""))
                else:
                    r = bug_report.delete(bug_cfg(), str(d.get("receipt") or ""))
                return self.send(200 if r.get("ok") else 400, r)
            except Exception:
                return self.send(500, support_error("不具合の提出"))
        if self.path == "/api/license/recheck":  # 拒否の帯から、鍵を入れたあとに照合し直す(許可の判定は license_check のまま。鍵の値は返さない)
            try:
                return self.send(200, license_report(license_check.check(VERSION)))
            except Exception:
                return self.send(500, support_error("利用の確認"))
        if self.path in ("/api/search", "/api/send", "/api/approve", "/api/manual", "/api/mail/preview", "/api/mail/send"):  # 実行系(探す・送る)。利用許可が無いときは止める。できたリストの閲覧・書き出しは止めない
            allowed, why = license_check.gate(VERSION)
            if not allowed:
                return self.send(403, {"error": why, "license": "denied"})
        if self.path == "/api/search":
            sync_ng_txt(ng_store)
            args = ["formlist.py", "--limit", str(lim), "--fetch-limit", str(flim), "--parallel", str(par)]
            if ng_store is not None:
                args += ["--ng-state-db", str(ng_store.db_path)]
            if d.get("resume_checks"):
                args.append("--resume-checks")
            if d.get("resume_search") and d.get("restart_search"):  # 画面の二者択一「前回の続きから探す / 最初からやり直す」。両方が来たら、どちらの意味か決められないので断る
                return self.send(400, {"error": "「前回の続きから探す」と「最初からやり直す」は、同時には選べません。どちらか一方を選んでください"})
            if d.get("restart_search"):  # 画面の「最初からやり直す」。付けなければ、同じエリア・業種は前回の続きから集める(探索履歴)
                args.append("--restart-search")
            if d.get("official_research"):
                args.append("--official-research")
            if d.get("include_large") is False:  # 既定は含める。画面で「大手・チェーンも含める」を外したときだけ外す
                args.append("--exclude-large")
            for purl in dict.fromkeys([str(d.get("portal") or "").strip()] + [str(x).strip() for x in (d.get("portals") or [])]):
                if purl:
                    args += ["--portal", purl]
            else:
                kws = [k for k in re.split(r"[\s、,，]+", d.get("keywords", "")) if k]
                if not d.get("area") or not (d.get("industries") or kws):
                    return self.send(400, {"error": "場所と、業種またはキーワードを入れてください"})
                if any(c in osm_source.KW_BAD for k in kws for c in k):
                    return self.send(400, {"error": "キーワードに記号（. * + ? ( ) [ ] { } | \\ \" ^ $）は使えません"})
                args += ["--area", d["area"]]
                for i in d.get("industries", []):
                    args += ["--industry", i]
                for k in kws:
                    args += ["--keyword", k]
                for s in d.get("sources") or ["google"]:
                    if s == "google_places_text":
                        return self.send(400, {"error": GOOGLE_PLACES_TEXT_BLOCK, "code": "google_places_text_blocked"})
                    if s not in ("osm", "google"):
                        return self.send(400, {"error": "取得元が不正です"})
                    args += ["--source", s]
            if d.get("ng", "").strip():
                bad = []
                for line in d["ng"].splitlines():
                    item = line.split("#")[0].strip()
                    if item and not add_ng_item(item, NGMARK, store=ng_store):
                        bad.append(item)
                if bad:
                    return self.send(400, {"error": "NGリストに入れられない行があります（広すぎる・形が違う）: " + " / ".join(bad[:5])})
                ng = OUT / "ng.txt"
                OUT.mkdir(exist_ok=True)
                ng.write_text(d["ng"], encoding="utf-8")
                args += ["--ng", str(ng)]
            blocked = google_block_until() if "google" in (d.get("sources") or ["google"]) else ""
            if blocked:  # 止められている間は、別枠も含めて始めない(formlist.py も同じ印を見て止まる)。先に理由を返す
                peer_log("search 断り 種類=google_blocked")
                return self.send(200, {"started": False, "error": google_block_message(blocked), "kind": "google_blocked"})
            ok = run_job("探す", args)
            if ok:
                SEARCH_COND["key"] = cond_key(d)
                return self.send(200, {"started": True})
            return self.send(200, busy_reply(d))
        if self.path == "/api/stop":
            # 手で止める。「探す」だけ止める(印のファイルを置くと、探す処理が次の区切り(検索1回・会社1社)で止まり、調べ終えた分でリストを作る)。送信は止めない
            if not SJOB["running"]:
                return self.send(200, {"stopped": False, "note": "止める「探す」処理は動いていません"})
            OUT.mkdir(exist_ok=True)
            (OUT / f"探す_止める_{PORT}.flag").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            return self.send(200, {"stopped": True, "note": "止める指示を出しました。調べ終えた分でリストを作って終わります（数十秒かかります）"})
        if self.path == "/api/plan":
            # 始める前の見積もり(Googleマップの検索が何件・何分か)
            import plan
            kws = [k for k in re.split(r"[\s、,，]+", d.get("keywords", "")) if k]
            return self.send(200, plan.plan_searches(str(d.get("area", "")).strip(), d.get("industries") or [], kws, lim))
        if self.path == "/api/history_status":
            # 「探しはじめる」を押す前に、この条件に前回の続きがあるかを返す(画面の「前回の続きから探す」の下に出す)
            return self.send(200, history_status(d))
        if self.path == "/api/newtab":
            pt, why, kind = start_peer_kind()
            if pt is None:
                peer_log(f"newtab 失敗 種類={kind}")
                return self.send(409, {"error": why, "kind": kind})
            peer_log(f"newtab 成功 枠={pt}")
            return self.send(200, {"url": f"http://127.0.0.1:{pt}/?peer=1", "port": pt})
        if self.path == "/api/update":
            # 最新版へ入れ替えて、起動し直す。探す・送るの最中(どのサーバーでも)は断る。入れ替えは、検査に全部通ったときだけ(updater.py)
            if JOB["running"] or SJOB["running"] or any((ports.job(p) or {}).get("running") for p in ports.POOL if p != PORT):
                return self.send(409, {"error": "探す・送る処理の最中は、更新できません。終わってから、もう一度この画面を開いてください"})
            if not UPDATE_BUSY.acquire(blocking=False):
                return self.send(409, {"error": "ほかの画面が、いま更新しています。数秒待ってください"})
            try:
                try:
                    r = updater.apply()
                except ValueError as e:
                    updater.write_status("失敗", f"更新できませんでした（前の版のまま）: {e}")
                    return self.send(200, {"updated": [], "error": str(e)})
                except Exception as e:
                    updater.write_status("失敗", f"更新に失敗しました（{type(e).__name__}: {e}）。前の版のまま")
                    return self.send(500, {"updated": [], "error": f"更新に失敗しました（{type(e).__name__}: {e}）。前の版のまま使えます"})
                UPDATE_CACHE.clear()
                if not r["updated"] and updater.build() == BUILD:
                    return self.send(200, {"updated": [], "restart": False})
                self.send(200, {"updated": r["updated"], "restart": True})
                for _j in (JOB, SJOB):
                    if _j.get("proc"):
                        _j["proc"].terminate()
                updater.relaunch(PORT, r["backup"], version=r.get("version", ""))
                threading.Timer(0.5, lambda: os._exit(0)).start()
                return
            finally:
                UPDATE_BUSY.release()
        if self.path == "/api/restart":
            # 終了して、同じ番号で起動し直す。探す・送るの最中は断る(タスクマネージャーを使わせない)
            if JOB["running"] or SJOB["running"]:  # 自分のサーバーだけを起動し直す。ほかの番号の検索は別のプロセスなので巻き込まない
                return self.send(409, {"error": "探す・送る処理の最中は、再起動できません。終わってから押してください"})
            self.send(200, {"ok": True})
            updater.relaunch(PORT, "")
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return
        if self.path == "/api/quit":
            if (JOB["running"] and JOB["name"] == "本番送信") or (d.get("if_idle") and (JOB["running"] or SJOB["running"])):
                _busy = JOB if JOB["running"] else SJOB
                return self.send(409, {"error": f"「{_busy['name']}」の最中は終了できません。終わってから押してください"})
            if d.get("if_idle") and HUMAN["asking"]:  # 送信承認の窓を出している間は、自動の終了(見張り・入れ替え)に応じない
                return self.send(409, {"error": "送信の承認の窓を出している間は終了できません"})
            for _j in (JOB, SJOB):
                if _j.get("proc"):
                    _j["proc"].terminate()
            self.send(200, {"ok": True})
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return
        if self.path == "/api/portals":
            items = load_portals()
            url = str(d.get("url", "")).strip()
            if d.get("delete"):
                save_portals([x for x in items if x.get("url") != url])
                return self.send(200, {"ok": True, "portals": load_portals()})
            name = str(d.get("name", "")).strip()[:40]
            if not re.match(r"(?i)https?://", url) or "." not in url:
                return self.send(400, {"error": "ポータルのURLが読み取れません(https:// で始まるアドレスを入れてください)"})
            if not any(x.get("url") == url for x in items):
                items.append({"name": name or url, "url": url})
                save_portals(items)
            return self.send(200, {"ok": True, "portals": items})
        if self.path == "/api/ng/undo":
            line = str(d.get("line", "")).strip()
            if ng_store is None:
                lines = ng_screen(ng_store)
                if line not in lines:
                    return self.send(400, {"error": "この行は画面から戻せません（自動で入った行は、そのままにしておきます）"})
            if not ng_undo_screen(line, ng_store):
                return self.send(400, {"error": "この行は、すでに別の操作で外れています。画面を開き直してください"})
            return self.send(200, {"ok": True})
        if self.path == "/api/ng":
            hp, form = str(d.get("hp", "")).strip(), str(d.get("form", "")).strip()
            name = str(d.get("name", ""))[:40]
            ok = ng_add_screen(hp, form, name, ng_store)
            if ok or (ng_store is None and ((hp and ng_list.is_ng(hp)) or (form and ng_list.is_ng_form(form)) or (form and ng_list.is_ng_email(form)))):
                return self.send(200, {"ok": True})
            return self.send(400, {"error": "この会社は送らないリストに入れられませんでした（ホームページの情報が足りません）"})
        if self.path == "/api/sender":
            old = json.loads(SENDER.read_text(encoding="utf-8-sig")) if SENDER.exists() else {}
            keep = ("会社名", "氏名", "フリガナ", "メール", "電話", "郵便番号", "住所", "件名", "署名", "資料URL", "計測キー")
            fill_only = bool(d.pop("_fill_only", False))  # 設定の文の適用(MCP の既定): 見本のまま・空の項目にだけ入れ、本人が直した項目は上書きしない(Q899)
            bad = [k for k in d if k not in keep]
            if bad:
                return self.send(400, {"error": "送信元にない項目です: " + "、".join(bad[:5])})
            new_key = d.pop("計測キー", None)  # 鍵は sender.json に入れず、応答にも出さない。ほかの検査が全部通ってから保存する
            skipped = []
            if fill_only:
                for k in list(d):
                    cur = str(old.get(k, ""))
                    if str(d[k]) != cur and not settings_guard.sender_fillable(cur, k):
                        skipped.append({"項目": k, "いまの値": cur, "届いた値": str(d[k]), "差分": settings_guard.diff_text(cur, str(d[k]))})
                        del d[k]
            if new_key is not None and not proposal_links.KEY_OK.fullmatch(str(new_key).strip()):
                return self.send(400, {"error": "計測キーは、英数字と - _ だけの32〜128文字です。設定の文のとおりに貼ってください"})
            url = str(d.get("資料URL", "")).strip()
            if url and not (re.fullmatch(r"https?://[^\s{}【】]+", url) or proposal_links.TEMPLATE_OK.fullmatch(url)):  # {識別} は https://ホスト/階層/?t={識別} の形だけ許す
                return self.send(400, {"error": "資料URLは https:// か http:// で始まる、空白のないURLにしてください（先方にそのまま届きます）"})
            if proposal_links.MARK in url and new_key is None:
                try:
                    proposal_links._secret()
                except RuntimeError as e:  # 鍵が無いまま保存すると、プレビューと送信で初めて止まる
                    return self.send(400, {"error": str(e)})
            if new_key is not None:
                proposal_links.save_key(new_key)
            old.update({k: str(v) for k, v in d.items()})  # 画面に出ない項目を消さず、決めた項目だけ文字で受ける
            new_text = json.dumps(old, ensure_ascii=False, indent=1)
            kept = None
            if SENDER.exists() and SENDER.read_text(encoding="utf-8-sig").strip() != new_text.strip():
                kept = settings_guard.backup(SENDER.parent, [SENDER])  # 変わる前の送信元を控える(上書きしても戻せる)
            SENDER.write_text(new_text, encoding="utf-8")
            # 保存した結果と、まだ送れない理由を同じ応答で返す(「保存しました」と「足りません」を別々に出さない。S2)
            files = outputs()
            try:
                tpl = templates_for(safe_out(files[0])) if files else templates_for(None)
            except OSError:
                tpl = templates_for(None)
            return self.send(200, {"ok": True, "problems": send_rules.sender_problems(old, list(tpl.values())), "skipped": skipped, "backup": str(kept) if kept else None})
        if self.path == "/api/openings":
            f = safe_out(str(d.get("file", "")))
            ops = d.get("openings")
            source = str(d.get("source") or "person")
            if not f or not str(f).endswith(".csv") or not isinstance(ops, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in ops.items()):
                return self.send(400, {"error": "リストのファイル名と、会社名ごとの書き出し（文字）を渡してください"})
            if source not in ("person", "ai"):
                return self.send(400, {"error": "書き出しの作り方(source)は person か ai です"})
            try:
                with filelock.locked(f):
                    rows = read_csv(f)
                    names = {r.get("企業名") for r in rows}
                    unknown = [k for k in ops if k not in names]
                    if unknown:
                        return self.send(400, {"error": "リストにない会社名です: " + "、".join(unknown[:5])})
                    for r in rows:
                        if r.get("企業名") in ops:
                            r["書き出し"] = ops[r["企業名"]].strip()
                            r[OPENER_COL] = OPENER_SOURCES[source]  # 画面は作り方の値だけを見て説明を出す(人が直した/AIが書き直した)
                    _write_rows(f, rows)
            except PermissionError:
                return self.send(409, excel_error(f))
            return self.send(200, {"ok": True, "updated": len(ops)})
        if self.path == "/api/approve":
            if not self.from_browser_screen():  # 人が開いた画面(ブラウザ)からの操作だけを受ける。送信元(Origin)は偽装できるので、画面を開いたときに渡した cookie も要る
                return self.send(403, {"error": "承認は、ツールの画面（ブラウザ）で押してください"})
            with LOCK:
                key_now, names = PREVIEW["key"], list(PREVIEW.get("names") or [])
                skipped_n = int(PREVIEW.get("skipped") or 0)
            if key_now is None:
                return self.send(400, {"error": "まだ「送る内容を出す」を押していません。先に押して、届く文を読みます"})
            # 画面が見せた送る内容と、いま有効な送る内容が同じときだけ(別の確認の承認にならない)。画面は頭12文字(key_short)を渡す。64文字そのままも受ける(AI・古い画面)
            if str(d.get("key", "")) not in (key_now[-1], short_key(key_now[-1])):
                return self.send(409, {"error": "送る内容が古くなりました。もう一度「送る内容を出す」を押します"})  # E-20
            # 承認は画面のボタンではなく、OSの確認ダイアログ(HTTPでは押せない)で人に聞く。人が必ず読む1回に、判断に要る情報を集める(D-4)
            sender = read_sender() or {}
            shown = "\n".join("・" + n for n in names[:10]) + (f"\n ほか {len(names) - 10} 社" if len(names) > 10 else "")
            what_ = "メールアドレス" if PREVIEW.get("channel") == "mail" else "問い合わせフォーム"
            body = ("次の %d 社の%sに、画面に表示した文面を送ります。\n%s\n\n送信元: %s %s\n送信元のメール: %s\n"
                    "送らない会社: %d 社(理由は画面に出ています)\n入力がうまくいかなかったフォームは送信ボタンを押さず、「フォームで送る会社」に回します(数は送り終えたあとに出ます)\n"
                    "確認番号: %s\n\n画面で届く文を読んでいない場合は「いいえ」を押してください。\n「はい」で承認します。承認は1回の送信で使い切ります。"
                    % (len(names), what_, shown, sender.get("会社名", ""), sender.get("氏名", ""), sender.get("メール", ""), skipped_n, short_key(key_now[-1])))
            if PREVIEW.get("channel") == "mail":
                body = re.sub(r"送信元のメール: [^\n]*\n", lambda _m: "実際の送信元(SMTP): " + str(PREVIEW.get("from") or "") + "\n", body)
                body = re.sub(r"入力がうまくいかなかったフォームは[^\n]*\n", "", body)
            HUMAN["asking"] += 1  # 承認の窓を出している間は、見張りが止めない(whoami の asking)
            try:
                _ok = approval.ask_human("AIまるなげ営業 本番送信の承認", body)
            finally:
                HUMAN["asking"] -= 1
                HUMAN["last_action"] = time.monotonic()
            if not _ok:
                APPROVED["key"] = None
                return self.send(200, {"ok": False, "declined": True, "message": "送りませんでした(確認で「いいえ」を押したため)"})  # E-16
            if PREVIEW["key"] != key_now:  # ダイアログの間に確認が変わった
                APPROVED["key"] = None
                return self.send(409, {"error": "送る内容が古くなりました。もう一度「送る内容を出す」を押します"})
            with LOCK:
                if PREVIEW["key"] != key_now:
                    APPROVED["key"] = None
                    return self.send(409, {"error": "送る内容が古くなりました。もう一度「送る内容を出す」を押します"})
                APPROVED["key"] = key_now
            return self.send(200, {"ok": True})
        if self.path in ("/api/outcome", "/api/manual_sent", "/api/reply"):
            name, url = str(d.get("company", "")).strip(), str(d.get("form", "")).strip()
            if not name:
                return self.send(400, {"error": "会社名を入れてください"})
            if self.path == "/api/outcome":
                if not isinstance(d.get("delivered"), bool):
                    return self.send(400, {"error": "届いたかどうか(delivered: true/false)を入れてください"})
                r = sales_records.set_outcome(LOG, name, url, d["delivered"], str(d.get("how", ""))[:200])
                return self.send(400 if "error" in r else 200, r)
            if self.path == "/api/manual_sent":
                return self.send(200, sales_records.manual_sent(LOG, name, url, str(d.get("how", ""))[:200]))
            where = mailto_address(str(d.get("hp", "")).strip() or url) if d.get("kind") == "不要" else ""  # mailto: はアドレス単位で登録する(無料メールのドメイン全体を止めない)
            memo = str(d.get("memo", ""))[:200]
            if where:
                memo = (memo + " " + NG_WHERE + where).strip()  # 送らない先を返事の記録に残す(送らない会社の一覧で、理由・日付と一緒に出す)
            r = sales_records.add_reply(name, url, str(d.get("kind", "")), str(d.get("text", "")), memo)
            if "error" in r:
                return self.send(400, r)
            if d.get("kind") == "不要":  # 送らないでと言われた会社は、記録と同じ操作で送らないリストに入れる(片方だけが抜けないように)
                added = (ng_store.add_domain_ng(where, "返事で不要と言われた", name) if ng_store is not None else ng_list.add(where, "返事で不要と言われた", name)) if where else None
                r["ng_added"] = bool(added)
                if not where:
                    r["note"] = "ホームページかフォームのアドレスが無いので、送らないリストには入れていません。formlist_ng_add で入れてください"
            return self.send(200, r)
        if self.path == "/api/ticket/consume":  # 送信を受け持つ autosend が、画面が発行した使い切りの鍵を確かめに来る
            return self.send(200, {"ok": approval.redeem(d.get("token"), d.get("expect"))})
        if self.path == "/api/approve/cancel":
            APPROVED["key"] = None
            approval.revoke_ticket()
            return self.send(200, {"ok": True})
        if self.path == "/api/sample":
            import sample_from_material
            try:
                sender = json.loads(SENDER.read_text(encoding="utf-8-sig")) if SENDER.exists() else {}
                return self.send(200, sample_from_material.make_sample(str(d.get("material", ""))[:20000], sender))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
        if self.path == "/api/message":
            MSG.mkdir(exist_ok=True)
            fill_only = bool(d.pop("_fill_only", False))  # 設定の文の適用(MCP の既定): 見本のまま・無い文面にだけ入れ、本人が直した文面は上書きしない(Q899)
            # 値は文面ファイルの本文(文字。MCP・以前の画面)か、画面の部分 {greeting, opener, body, signature}(D-10。保存のときに組み立てる)
            bad = [k for k in d if not (k.replace("・", "").replace("_", "").isalnum() or k == "default")]
            if bad or not all(isinstance(v, str) or (isinstance(v, dict) and isinstance(v.get("body", ""), str)) for v in d.values()):
                return self.send(400, {"error": "保存できない名前か中身です: " + "、".join(bad[:5])})
            saved = {k: (join_message(v) if isinstance(v, dict) else v) for k, v in d.items()}
            skipped = []
            if fill_only:
                for k, v in list(saved.items()):
                    p = MSG / f"{k}.txt"
                    if not p.exists():
                        continue
                    cur = p.read_text(encoding="utf-8-sig")
                    tpl_p = HERE / "文面ひな形" / f"{k}.txt"
                    tpl = tpl_p.read_text(encoding="utf-8-sig") if tpl_p.exists() else None
                    if settings_guard._norm(cur) != settings_guard._norm(v) and not settings_guard.message_fillable(cur, tpl):
                        skipped.append({"文面": k, "いまの値": cur, "届いた値": v, "差分": settings_guard.diff_text(cur, v)})
                        del saved[k]
            kept = settings_guard.backup(MSG.parent, [MSG / f"{k}.txt" for k, v in saved.items()
                                                       if (MSG / f"{k}.txt").exists() and settings_guard._norm((MSG / f"{k}.txt").read_text(encoding="utf-8-sig")) != settings_guard._norm(v)])  # 変わる文面だけ、変わる前を控える
            for k, v in saved.items():
                (MSG / f"{k}.txt").write_text(v, encoding="utf-8")
            # 見本の文字が残っていても保存はできる(途中で保存したい人のため)。送る内容を出すところで止まる。ここでは何が残っているかを返す
            probs = message_problems({("標準" if k == "default" else k): v for k, v in saved.items()})
            return self.send(200, {"ok": True, "problems": probs, "parts": {k: split_message(v) for k, v in saved.items()}, "skipped": skipped, "backup": str(kept) if kept else None})
        if self.path == "/api/message/pattern":
            # 業種の型(パターン)ごとの文面テンプレートを1つ保存する: {"number": 番号, "text": 本文}。既定(_fill_only)は、専用ファイルが無い番号にだけ入れる
            import patterns
            try:
                number = int(d.get("number"))
            except (TypeError, ValueError):
                number = 0
            if number not in patterns.PATTERN_NAMES:
                return self.send(400, {"error": "パターン番号は、1から13で入れてください"})
            text = d.get("text")
            if not isinstance(text, str) or not text.strip():
                return self.send(400, {"error": "文面の本文が空です"})
            current_file = patterns.pattern_file(number)
            if current_file is not None and d.get("_fill_only"):
                cur = current_file.read_text(encoding="utf-8-sig")
                if settings_guard._norm(cur) != settings_guard._norm(text):
                    return self.send(200, {"ok": True, "skipped": [{"パターン": number, "いまの値": cur, "届いた値": text, "差分": settings_guard.diff_text(cur, text)}], "backup": None})
                return self.send(200, {"ok": True, "skipped": [], "backup": None})
            kept = settings_guard.backup(patterns.PATTERN_DIR.parent.parent, [current_file]) if current_file is not None else None  # 変わる前を、設定の控えへ残す
            saved = patterns.save_pattern(number, text)
            return self.send(200, {"ok": True, "file": saved.name, "backup": str(kept) if kept else None, "pattern_status": patterns.pattern_status(), "patterns_missing": patterns.patterns_missing()})
        if self.path == "/api/message/subject":
            # 業種の型(パターン)ごとの件名を、文面と同じ画面で直す: {"industry": 業種名, "subject": 件名(空なら初期値に戻す)}
            import patterns
            ind, subj = str(d.get("industry", "")), str(d.get("subject", "")).strip()
            n = patterns.pattern_for(ind) if ind in osm_source.INDUSTRY_TAGS else None
            if not n:
                return self.send(400, {"error": "この業種には、業種の型ごとの件名がありません（基本の文面の件名は「送信元」の件名です）"})
            if "\n" in subj or "\r" in subj or len(subj) > 100:
                return self.send(400, {"error": "件名は、1行・100文字までで入れてください"})
            patterns.set_subject(n, subj)
            return self.send(200, {"ok": True, "subject": patterns.subject_for_industry(ind)})
        if self.path == "/api/message/expand":
            # 業種ごとの文面の切り替え(希望者だけ): {"on": true|false} は使う/使わないの印、{"make": true} は標準(原本)を写して、まだ専用の無い業種に専用の文面を作る
            import autosend
            if not (MSG / "default.txt").exists():
                return self.send(400, {"error": "先に標準の文面を保存してください"})
            if "on" in d:
                (MSG / autosend.EXPAND_OFF).unlink(missing_ok=True) if d["on"] else (MSG / autosend.EXPAND_OFF).write_text("業種ごとの文面を使わない\n", encoding="utf-8")
            made = []
            if d.get("make"):
                for n in osm_source.INDUSTRY_TAGS:
                    if not (MSG / f"{n}.txt").exists():  # 作り済みの業種は上書きしない
                        shutil.copy2(MSG / "default.txt", MSG / f"{n}.txt")
                        made.append(n)
            return self.send(200, {"ok": True, "expand_on": not (MSG / autosend.EXPAND_OFF).exists(), "made": made})
        if self.path in ("/api/message/move", "/api/message/delete"):
            # 業種の文面の引き継ぎ(古い名前→新しい分類。コピーなので元は残る)と、削除(専用をやめて「標準」に戻す。消さずに 文面/_削除済み に移す)
            info = message_info()
            src = str(d.get("from") or d.get("name") or "")
            known = set(info["industries"]) | {o["name"] for o in info["orphans"]}
            if src == "default" or src not in known or not (MSG / f"{src}.txt").exists():
                return self.send(400, {"error": "その文面は、ありません（「標準」は削除も引き継ぎもできません）"})
            if self.path.endswith("/delete"):
                bin_dir = MSG / "_削除済み"
                bin_dir.mkdir(exist_ok=True)
                os.replace(MSG / f"{src}.txt", bin_dir / f"{src}_{time.strftime('%Y%m%d_%H%M%S')}.txt")
                return self.send(200, {"ok": True, "note": f"「{src}」の専用の文面をやめました。この業種には、割り当てたパターンの文面(無ければ「標準」)が使われます（前の文面は 文面/_削除済み に残してあります）"})
            to = str(d.get("to") or "")
            if to not in info["industries"]:
                return self.send(400, {"error": "引き継ぎ先は、いまの業種の一覧から選んでください"})
            if (MSG / f"{to}.txt").exists():
                return self.send(409, {"error": f"「{to}」には、すでに専用の文面があります。上書きしません。先にそちらをやめるか、中身を直してください"})
            shutil.copy2(MSG / f"{src}.txt", MSG / f"{to}.txt")
            return self.send(200, {"ok": True, "note": f"「{src}」の文面を、「{to}」の専用の文面にコピーしました（元の文面も残してあります）。顧客に届く文面なので、中身を読んで、必要なら直して保存してください"})
        if self.path == "/api/manual":  # 手で送る: フォームを開いて入力だけ自動。送信ボタンは押さない(人が画面を閉じるまで待つ)
            p, err = list_or_error(d.get("file", ""))
            if err:
                return self.send(*err)
            name = str(d.get("name", "")).strip()
            if not name:
                return self.send(400, {"error": "フォームで送る会社を選んでください"})
            probs = send_rules.sender_problems(read_sender())
            if probs:
                return self.send(400, problem_error(probs))
            import autosend
            _row, _bodies, problem = autosend.manual_precheck(str(p), str(SENDER), str(MSG), name)  # 始める前に、書き出し空欄・フォームURLなしを確かめる(ジョブを走らせて終了コードだけ見せない)
            if problem:
                return self.send(400, {"error": f"「{name}」の「フォームで送る(自動挿入)」は始められません。理由: {problem[0]}。直し方: {problem[1]}", "field": LIST_FIELD, "fix": "list"})
            return self.send(200, {"started": run_job("手動入力", ["autosend.py", str(p), "--sender", str(SENDER), "--message", str(MSG), "--manual-company", name])})
        if self.path == "/api/send/stop":  # 送信の途中で止める。今の1社は終え、次の会社に進まない。残りは未送信のまま残り、次の「送る内容を出す」に出る(承認・全文確認・確認番号は変えない)
            if not (JOB["running"] and JOB["name"] == "本番送信"):
                return self.send(200, {"stopped": False, "note": "止める送信は動いていません"})
            OUT.mkdir(exist_ok=True)
            send_stop_flag().write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            return self.send(200, {"stopped": True, "note": "止める指示を出しました。いま送っている1社が終わったら止まります。残りの会社は送っていないまま残ります"})
        if self.path == "/api/send/dismiss":  # 送り終えた表示(s7)を閉じて、次の送る準備(s3)へ。送信の記録は消さない
            SEND_INFO["dismissed"] = True
            return self.send(200, {"ok": True})
        if self.path == "/api/render_sample":
            return self.render_sample(d)
        if self.path == "/api/send":
            return self.send_api(d, mx, ng_store)
        if self.path in MAIL_POST:
            return mail_post(self, d, mx, ng_store)
        self.send(404, {"error": "なし"})


# ---- メール送信の道(送り方の選択・設定画面・1社ずつ/選んだ会社の確認と送信)。メール送信の部品(mail_channel)が無い版では、どれも使えない ----
MAIL_GET = ("/api/mail/status", "/api/mail/plan")
MAIL_POST = ("/api/choice", "/api/choice/rule", "/api/mail/settings", "/api/mail/test", "/api/mail/preview", "/api/mail/send")


def _mail_channel():
    """メール送信の部品。公開版には入れていないので importlib で読む(無いときは _mail_available() が先に止める)"""
    import autosend
    return autosend._mail_channel()


def _mail_available():
    import autosend
    return autosend._mail_available()


def _mail_raw_settings():
    """smtp.json を、足りなくても壊れていても読む(画面に「何が足りないか」を出すため)。無ければ {}"""
    mail_channel = _mail_channel()
    try:
        d = json.loads(mail_channel.SMTP_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def mail_status():
    """メール送信の部品があるか・設定が揃っているか・二重送信を避ける規則が有効か。パスワードの値は返さない(入っているかだけ)"""
    out = {"available": _mail_available(), "rule": channel_choice.rule_enabled()}
    if not out["available"]:
        return out
    mail_channel = _mail_channel()
    raw = _mail_raw_settings()
    shown = {"host": str(raw.get("host") or ""), "port": raw.get("port") or 465, "security": str(raw.get("security") or "ssl"),
             "user": str(raw.get("user") or ""), "from_addr": str(raw.get("from_addr") or ""), "from_name": str(raw.get("from_name") or ""),
             "has_password": bool(str(raw.get("password") or "").strip())}
    try:
        mail_channel.load_settings()
        return dict(out, configured=True, missing=[], problem="", **shown)
    except mail_channel.MailError as e:
        names = {"host": "サーバー", "user": "ユーザー名", "password": "パスワード"}
        missing = [names[k] for k in ("host", "user", "password") if not str(raw.get(k) or "").strip()]
        return dict(out, configured=False, missing=missing, problem=str(e), **shown)


def mail_annotate(rows):
    """結果の各行に、メールで送れるアドレス(_mail)・会社の鍵(_key)・選んだ送り方(_choice)を足す。メール送信の部品が無い版では何も足さない"""
    import autosend
    if not autosend._mail_available():
        return rows
    out = []
    for r in rows:
        r = dict(r)
        r["_mail"] = autosend._mail_of(r) if autosend._mail_addr_ok(r) else ""
        r["_key"] = autosend._choice_key(r)
        r["_choice"] = channel_choice.get(r["_key"])
        out.append(r)
    return out


def _mail_only(values):
    """画面が送ってきた会社名の並び。空の項目は捨てる。無ければ None(対象の全社)"""
    return [str(x).strip() for x in (values or []) if str(x).strip()] or None


def _mail_plan(p, supp, mx, ng_store, only, ignore_choice=False):
    import autosend
    return autosend.build_mail_plan(str(p), supp, ng_store, mx, str(MSG), read_sender() or {}, only=set(only) if only else None, ignore_choice=ignore_choice)


def mail_get(handler, u, ng_store):
    if u.path == "/api/mail/status":
        return handler.send(200, mail_status())
    if not _mail_available():
        return handler.send(404, {"error": "この版にはメール送信の部品が入っていません"})
    q = parse_qs(u.query)
    one = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731
    p, err = list_or_error(one("file"))
    if err:
        return handler.send(*err)
    try:
        mx = count(one("max") or None, None, 1, None)
    except ValueError:
        return handler.send(400, count_error())
    supp = one("supp") == "1"
    only = _mail_only(q.get("only"))
    try:
        probs = gate_problems(read_sender(), templates_for(p))
        if probs:
            return handler.send(400, problem_error(probs))
        draft = one("draft") == "1"  # 件名と本文を見るだけ(送り方を選ぶ前でも、会社を指定すれば作れる)
        pl = _mail_plan(p, supp, mx, ng_store, only, ignore_choice=draft)
    except PermissionError:
        return handler.send(409, excel_error(p))
    except Exception:
        return handler.send(400, support_error("mail_plan"))
    key = (str(p), p.stat().st_mtime_ns, mx, supp, fingerprint(), pl["digest"])
    valid = (not draft) and PREVIEW["key"] is not None and pl["digest"] and PREVIEW["key"] == key
    items = [{"name": r[0], "addr": r[1], "industry": r[2], "subject": s, "text": b} for r, s, b in zip(pl["todo"], pl["subjects"], pl["bodies"])]
    who = pl["identity"]
    return handler.send(200, {"sendable": items, "skipped": pl["skipped"], "settings_error": pl["settings_error"],
                              "identity": {k: who[k] for k in ("from_addr", "from_name", "host", "port", "security")} if who else None,
                              "key_short": short_key(pl["digest"]) if valid else None, "key": pl["digest"] if valid else None,
                              "window": send_rules.window_refusal()})


def _mail_save_settings(d):
    mail_channel = _mail_channel()
    old = _mail_raw_settings()
    new = {"host": str(d.get("host") or "").strip()[:200], "port": d.get("port") or 465, "security": str(d.get("security") or "ssl").strip(),
           "user": str(d.get("user") or "").strip()[:200], "password": str(d.get("password") or "") or str(old.get("password") or ""),
           "from_addr": str(d.get("from_addr") or "").strip()[:200], "from_name": str(d.get("from_name") or "").strip()[:200]}
    tmp = mail_channel.SMTP_FILE.with_name(mail_channel.SMTP_FILE.name + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 作る時点から持ち主だけの権限(Windows では効かないことがある。ベストエフォート)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(new, ensure_ascii=False, indent=1))
    try:
        mail_channel.load_settings(tmp)  # 形の検査(足りない欄は欄の名前つきで MailError)
    except mail_channel.MailError:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, mail_channel.SMTP_FILE)


def mail_post(handler, d, mx, ng_store):
    path = handler.path
    if path in ("/api/choice", "/api/choice/rule"):
        if not _mail_available():
            return handler.send(404, {"error": "この版にはメール送信の部品が入っていません"})
        if JOB["running"] and JOB["name"] == "本番送信":
            return handler.send(409, {"error": "いま送っている最中は、送り方の選択を変えられません。送り終わってから変えます"})
        if path == "/api/choice/rule":
            if not isinstance(d.get("enabled"), bool):
                return handler.send(400, {"error": "有効か無効か(enabled: true/false)を入れてください"})
            channel_choice.set_rule(d["enabled"])
            return handler.send(200, {"ok": True, "rule": channel_choice.rule_enabled()})
        name, hp = str(d.get("name") or "").strip(), str(d.get("hp") or "").strip()
        if not name:
            return handler.send(400, {"error": "会社名を入れてください"})
        try:
            channel_choice.set_choice(channel_choice.company_key(name, hp), str(d.get("channel") or ""), name)
        except ValueError:
            return handler.send(400, {"error": "送り方は「フォーム」か「メール」のどちらかです"})
        return handler.send(200, {"ok": True})
    if not _mail_available():
        return handler.send(404, {"error": "この版にはメール送信の部品が入っていません"})
    import autosend
    mail_channel = _mail_channel()
    if path == "/api/mail/settings":
        if JOB["running"] and JOB["name"] == "本番送信":
            return handler.send(409, {"error": "いま送っている最中は、メール送信の設定を変えられません。送り終わってから変えます"})
        try:
            _mail_save_settings(d)
        except mail_channel.MailError as e:
            return handler.send(400, {"error": str(e), "field": "mail_settings"})
        return handler.send(200, dict(mail_status(), ok=True))
    if path == "/api/mail/test":
        try:
            settings = mail_channel.load_settings()
        except mail_channel.MailError as e:
            return handler.send(400, {"error": str(e), "field": "mail_settings"})
        ok, memo = mail_channel.check_login(settings)
        return handler.send(200, {"ok": ok, "message": "接続と認証ができました(メールは送っていません)" if ok else memo})
    # 以降は確認(preview)と本番(send)。フォームの send_api と同じ止め方・同じ承認の印(PREVIEW/APPROVED)を使う
    real = path == "/api/mail/send"
    p, err = list_or_error(d.get("file", ""))
    if err:
        return handler.send(*err)
    only = _mail_only(d.get("only"))
    if d.get("only") is not None and only is None:
        return handler.send(400, {"error": "送る会社を選んでください"})
    try:
        probs = gate_problems(read_sender(), templates_for(p))
    except PermissionError:
        return handler.send(409, excel_error(p))
    if probs:
        return handler.send(400, problem_error(probs))
    st = mail_status()
    if not st.get("configured"):
        return handler.send(400, {"error": "メール送信の設定が揃っていません(足りないところ: " + "・".join(st.get("missing") or ["形"]) + ")。「メール送信の設定」で入れてから送ります",
                                  "field": "mail_settings", "fix": "mail_settings", "missing": st.get("missing") or []})
    busy = busy_error()
    if busy:
        return handler.send(409, busy)
    supp = bool(d.get("supp"))
    args = ["autosend.py", str(p), "--channel", "mail", "--sender", str(SENDER), "--message", str(MSG)]
    if mx is not None:
        args += ["--max", str(mx)]
    if ng_store is not None:
        args += ["--ng-state-db", str(ng_store.db_path)]
    if supp:
        args.append("--include-supplemented")
    for n in only or []:
        args += ["--only-company", n]
    sync_ng_txt(ng_store)
    if real and (ng_store is None or NG_MIGRATION["error"] or (_NG_SYNC and ng_store.migration_status() != "canonical")):
        return handler.send(409, {"error": "送らない会社の一覧を読み込めなかったので、本番は送れません。ツールを終了して起動し直します"})
    try:
        pl = _mail_plan(p, supp, mx, ng_store, only)
        key = (str(p), p.stat().st_mtime_ns, mx, supp, fingerprint(), pl["digest"])
        parts = key_parts(p, mx, supp)
    except PermissionError:
        return handler.send(409, excel_error(p))
    except Exception:
        return handler.send(400, support_error("mail_send"))
    if not real:
        if key[-1] is None:
            why = "・".join(dict.fromkeys(s["text"] for s in pl["skipped"][:3])) or "メールで送る会社が選ばれていません(フォームとメールの両方がある会社は、送り方を選ぶと送れます)"
            return handler.send(409, {"error": "メールで送れる会社がありません。" + why, "code": "nothing_to_send"})
        skipped_n = len(pl["skipped"])

        who_ = pl["identity"] or {}
        from_text = f"{who_.get('from_name') or ''} <{who_.get('from_addr')}> ({who_.get('host')}:{who_.get('port')})"

        def ok(k=key, pt=parts, n=skipped_n, ft=from_text):
            if f"確認番号: {k[-1]}" in JOB["lines"]:
                set_preview(k, pt, "mail")
                PREVIEW["skipped"] = n
                PREVIEW["from"] = ft
        started = run_job("確認", args + ["--preview"], on_ok=ok)
        return handler.send(200, {"started": True} if started else busy_error() or {"started": False})
    if d.get("confirm") != "送信":
        return handler.send(400, {"error": "「届く文を読みました」にチェックを入れてから送ります"})
    if PREVIEW["key"] is None:
        return handler.send(400, {"error": "まだ「送る内容を出す」を押していません。先に押して、届く文を読みます"})
    if PREVIEW["key"] != key:
        return handler.send(400, {"error": changed_text(parts)})
    if APPROVED["key"] != key:
        return handler.send(400, {"error": "人の承認がありません。画面で届く文を読み、送るボタンを押して、画面の外に出る確認で「はい」を押します"})
    total = len(PREVIEW.get("names") or [])
    PREVIEW.update(key=None, names=[], parts={})
    APPROVED["key"] = None
    args += ["--send", "--expect", key[-1]]
    token = approval.issue_ticket(key[-1])
    SEND_INFO.update(total=total, started=time.strftime("%Y-%m-%d %H:%M"), dismissed=False, interrupted=False)
    started = run_job("本番送信", args, "送信\n", extra_env={approval.TICKET_ENV: token, approval.PORT_ENV: str(PORT), approval.UI_TOKEN_ENV: TOKEN})
    if not started:
        approval.revoke_ticket()
        SEND_INFO["dismissed"] = True
        return handler.send(409, busy_error() or {"error": "いまほかの処理が動いています。終わってから押します", "started": False})
    return handler.send(200, {"started": True})


PAGE = (HERE / "ui.html").read_text(encoding="utf-8")  # 画面。デザインの変更はこのファイルだけで済む
_PAGE_MTIME = (HERE / "ui.html").stat().st_mtime_ns


def page():
    """画面(ui.html)を返す。ファイルが差し替わっていたら読み直す(動いたままでも、最新の画面が出る)"""
    global PAGE, _PAGE_MTIME
    m = (HERE / "ui.html").stat().st_mtime_ns
    if m != _PAGE_MTIME:
        PAGE = (HERE / "ui.html").read_text(encoding="utf-8")
        _PAGE_MTIME = m
    return PAGE


def _busy_anywhere():
    return bool(JOB["running"] or SJOB["running"] or any((ports.job(p) or {}).get("running") for p in ports.POOL if p != PORT))


def auto_update_once(why):
    """画面を開かなくても、公開の最新の版へ追従する。古いときだけ入れ替えて起動し直す。探す・送るの最中は何もしない(次の確認でやり直す)。
    入れ替えは updater.apply(検査に通ったときだけ書く・立ち上がらなければ前の版へ戻す)をそのまま使う。入れ替えたら True"""
    if _busy_anywhere():
        return False
    r = updater.check()
    UPDATE_CACHE.update(at=time.time(), result=r)
    if r.get("state") in ("未確認", "見送り", "失敗", "最新"):
        updater.write_status(r["state"], r.get("message", ""))  # 常駐の確認の結果を残す(未確認・見送りが無言にならない)
    if r.get("state") != "古い":
        return False
    if not UPDATE_BUSY.acquire(blocking=False):
        return False
    try:
        if _busy_anywhere():
            return False
        try:
            res = updater.apply()
        except Exception as e:
            updater.write_status("失敗", f"自動更新に失敗しました（{type(e).__name__}: {e}）。前の版のまま動いています")
            return False
        UPDATE_CACHE.clear()
        if not res["updated"] and updater.build() == BUILD:
            return False
        try:
            with open(HERE / "出力" / "更新の記録.txt", "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + f" 自動更新（{why}）: {len(res['updated'])}個のファイルを入れ替えて起動し直します\n")
        except OSError:
            pass
        updater.relaunch(PORT, res["backup"], version=res.get("version", ""))
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return True
    finally:
        UPDATE_BUSY.release()


def auto_update_loop():
    """起動から少し待って1回(起動時)、そのあとは一定の間隔(定期)で、auto_update_once を呼ぶ"""
    first = float(os.environ.get("FORMLIST_AUTO_UPDATE_FIRST") or 20)
    every = float(os.environ.get("FORMLIST_AUTO_UPDATE_EVERY") or 30 * 60)  # 古い画面のまま使われないよう30分ごと(処理中は入れ替えない)
    time.sleep(first)
    why = "起動時"
    while True:
        try:
            if auto_update_once(why):
                return
        except Exception:
            pass
        why = "定期"
        time.sleep(every)




if __name__ == "__main__":
    NO_BROWSER = bool(os.environ.get("FORMLIST_NO_BROWSER")) or "--no-browser" in sys.argv  # 画面を開かずに起動する(MCP・ログイン時の自動起動)
    OUT.mkdir(exist_ok=True)
    cap_log(OUT / "app_起動ログ.txt")  # 起動のたびに増える記録に上限を置く
    cap_log(OUT / SUPPORT_NAME, 300_000, 100_000)
    _target, _why = launch_target()
    _err_start = 0
    if _why:  # 振り向けなかった理由(導入先が壊れている・古い案内の FORMLIST_PORT が入っている)を、黙らずに出す
        launch_notice(_why, show=not NO_BROWSER and not os.environ.get("FORMLIST_NO_BROWSER"))
    if _target is not None:  # 導入先のコピーを起動する(古いフォルダ・別の場所のコピーから起動されても、動くのは導入先の1つ)
        _args = ["-X", "utf8", str(_target / "app.py")] + (["--no-browser"] if NO_BROWSER else [])
        # 導入先が起動の途中で落ちたときの原文(黙って消さない)。導入先の側に置く: 起動した導入先は常駐してこのファイルを掴み続けるので、
        # 呼んだ古いコピーの下に置くと、古いフォルダを消す・移すができなくなる(2026-11-10 破壊検証 3-3)
        _errf = _target / "出力" / "起動エラー_振り向け.txt"
        try:
            _errf.parent.mkdir(parents=True, exist_ok=True)
            _ef0 = open(_errf, "ab")  # 追記: 前の振り向けで常駐中の導入先が同じファイルに書いた原文を消さない(破壊検証4回目 2-3)
            if _errf.stat().st_size > 200_000:
                _ef0.close()
                _ef0 = open(_errf, "wb")  # 大きくなりすぎたら空にしてから書く(常駐中の導入先の原文は失う。上限の代わり)
        except OSError:
            import tempfile
            _errf = Path(tempfile.gettempdir()) / f"formlist_起動エラー_振り向け_{os.getpid()}.txt"
            _ef0 = open(_errf, "ab")
        try:
            _ef0.seek(0, 2)
            _err_start = _ef0.tell()  # 今回の起動の原文はここから(前の原文の Traceback を今回のものと読まない)
            with _ef0 as _ef:
                _proc = subprocess.Popen([_pythonw()] + _args, cwd=_target, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                                         stdin=subprocess.DEVNULL, stdout=_ef, stderr=_ef, creationflags=NO_WINDOW,
                                         start_new_session=(os.name != "nt"))
        except OSError as e:
            _proc = None
            launch_notice(f"導入先のコピー({_target})を起動できませんでした({type(e).__name__})。このフォルダ({HERE})で起動します", show=not NO_BROWSER)
        if _proc is not None:
            # 振り向けた先が、画面の番号で実際に答えるまで待つ(R6: ファイルがそろっていても、起動すると落ちる壊れ方がある)
            _wait = float(os.environ.get("FORMLIST_REDIRECT_WAIT") or 20)
            _deadline, _up = time.time() + _wait, False
            while time.time() < _deadline and not _up:
                time.sleep(0.5)
                # 試験の入口の番号(FORMLIST_ENTRY_PORT)のときは、その番号だけを見る(試験が本物の 8780〜8782 に触れない。破壊検証 3-11)
                for _pt in [PORT] + ([] if os.environ.get("FORMLIST_ENTRY_PORT") else [x for x in ports.POOL if x != PORT]):
                    _j = ports.job(_pt, timeout=1) or {}
                    try:
                        _up = bool(_j.get("here")) and Path(_j["here"]).resolve() == _target.resolve()
                    except OSError:
                        _up = False
                    if _up:
                        break
                if not _up and _proc.poll() is not None and _proc.returncode != 0:
                    break  # 導入先が落ちた。待たずに切り替える
            if _up:
                launch_notice(f"このフォルダ({HERE})ではなく、導入先のコピー({_target})を起動しました", show=False)
                sys.exit(0)
            _rc = _proc.poll()
            try:
                _err = _errf.read_bytes()[_err_start:].decode("utf-8", "replace")
            except OSError:
                _err = ""
            # 応答が無いときの分け方(2026-11-10 破壊検証 3-1)。導入先は、別のコピーが作業中なら自分で理由(作業中のため止めていません)を出して
            # 終了コード1で止まる。それを「落ちた」と読んで「起動しませんでした・導入の1行をもう一度」を重ねて出さない
            if _rc is None:  # 導入先がまだ動いている(起動に時間がかかっている、または自分で理由の窓を出している)。表示は導入先に任せる
                launch_notice(f"導入先のコピー({_target})が{int(_wait)}秒たってもまだ起動の途中です。表示は導入先に任せ、このフォルダ({HERE})では起動しません", show=False)
                sys.exit(0)
            if _rc == 0:
                launch_notice(f"導入先のコピー({_target})が理由を残して終了しました。このフォルダ({HERE})では起動しません", show=False)
                sys.exit(0)
            if _rc == EXIT_SHOWN:  # 導入先が自分で理由を表示して止まった(作業中・別のアプリ)ことを、導入先の側が終了コードで示したときだけ黙る
                launch_notice(f"導入先のコピー({_target})が理由を表示して止まりました(終了コード {_rc})。このフォルダ({HERE})では起動しません", show=False)
                sys.exit(EXIT_SHOWN)
            # それ以外の終わり方は、Traceback の有無を問わず落ちたとみなし、このフォルダで起動する(強制終了・Fatal Python error は Traceback を残さない。
            # 分からないときに黙る側へ倒さない。2026-11-10 破壊検証4回目 2-1)
            _tail = [x for x in _err.strip().splitlines() if x.strip()][-1:]
            launch_notice(f"導入先のコピー({_target})が起動の途中で終了しました(終了コード {_rc}{'・' + _tail[0] if _tail else '・原文なし'})。"
                          f"このフォルダ({HERE})で起動します。導入の1行をもう一度実行すると直ります", show=not NO_BROWSER)
    import socket
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            st = ports.state(PORT, str(HERE))
            _occ = (ports.job(PORT) or {}).get("here") if st == "other_copy" else None
            if st == "other_copy" and _occ and installed_dir().exists() and Path(_occ).resolve() == installed_dir().resolve():
                # 導入先のコピーが動いている。導入先ではないこのフォルダが、導入先を止めて居座ることはしない(振り向けに失敗したときの逆転を防ぐ)
                launch_notice(f"{PORT}番では導入先のコピー({_occ})が動いています。そちらの画面を開きます", show=False)
                if not NO_BROWSER:
                    webbrowser.open(f"http://127.0.0.1:{PORT}/")
                sys.exit(0)
            if st == "other_copy":  # 別のフォルダに入れた、このツールの古いコピーが居座っている。何も動いていなければ、止めて入れ替える(番号を変えない=画面のURLを変えない)
                if replace_other_copy(PORT):
                    st = "free"
                    launch_notice(f"{PORT}番に居座っていた別のフォルダのコピーを止めて、このフォルダ({HERE})で起動し直します", show=False)
                else:
                    o = ports.job(PORT)
                    if o is None or o.get("running") is not False:
                        what = f"「{o.get('name') or '処理'}」を実行中" if (o or {}).get("running") else "作業中かどうか確かめられない状態"
                    else:
                        what = "止める手続きに応じない状態"
                    then = ("終わったら、見張りが5分ごとに確かめて入れ替えます。" if watchdog_registered() else "")
                    launch_notice(f"{PORT}番では、別のフォルダ({(o or {}).get('here') or '不明'})のこのツールが{what}のため、止めていません。"
                                  f"{then}急ぐときは、その画面で「ツールを終了する」を押してから、もう一度起動してください",
                                  show=not NO_BROWSER)
                    if not NO_BROWSER:
                        webbrowser.open(f"http://127.0.0.1:{PORT}/")  # 作業中の画面を開く(そのコピーが今回の版以降なら、画面の隅に版とフォルダが出る。古い版には隅の表示が無い)
                    sys.exit(EXIT_SHOWN)
            if st == "foreign":
                launch_notice(f"{PORT}番は、このツールではない別のアプリ(または応答しない古い版のこのツール)が使っています。止めていません。"
                              "使っていないアプリを閉じてから、もう一度起動してください", show=not NO_BROWSER)
                sys.exit(EXIT_SHOWN)
            if st == "ours":
                if not NO_BROWSER:  # MCP やログイン時の自動起動のときは画面を開かない
                    webbrowser.open(f"http://127.0.0.1:{PORT}/")  # すでに動いている。新しく起動せず、画面だけ開く
                sys.exit(0)
    init_ng_store()
    _NG_SYNC = True
    ensure_ng_migrated(_NG_STORE)
    if NG_MIGRATION["error"]:
        print("送らない会社の引っ越しに失敗しました。本番の送信は止まります:", NG_MIGRATION["error"])
    threading.Thread(target=ensure_watchdog_task, daemon=True).start()  # 見張りの登録の自己修復(R4)
    license_check.start_background(VERSION)  # 起動時と24時間ごとに利用許可を照合する(失敗してもツールは止めない)
    muse_link_apply()  # AI設定が muse のときだけ Muse との外向き接続を始める(既定の claude_code では何もしない)
    threading.Thread(target=auto_update_loop, daemon=True).start()  # 画面を開かなくても、起動時と6時間ごとに最新の版へ追従する
    srv = Srv(("127.0.0.1", PORT), H)
    restore_interrupted()  # 番号が空いていて、自分がサーバーになると決まった後にだけ戻す(使用中の起動で、動いている送信の記録を消さない)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"ブラウザで {url} を開いてください。終わるときはこの黒い画面を閉じます。")
    if not NO_BROWSER:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


