"""AIまるなげ営業(フォームリスト作成ツール)をブラウザで使う。実行環境: Windows / Python 3.9+。標準ライブラリ中心。
起動: py app.py （ブラウザが自動で開く）。自分のPCの中だけで動く（127.0.0.1）。外には公開しない。
できること: 1 探す（エリア・業種・ポータルURL・NGリスト） 2 結果を見る 3 送る（試走→本番。文面編集・送信元の保存・送信履歴）
中身は formlist.py と autosend.py をそのまま呼ぶ。画面はその進み具合を映す。
安全: 本番送信は、画面に出た文面の全文を見て「送信」と入力したときだけ動く（autosend.py の確認と同じ）。"""
import csv
import hashlib
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote

import time
import filelock
import ng_list
import osm_source
import state
from ng_state_store import NgStateStore

HERE = Path(__file__).resolve().parent
OUT = HERE / "出力"
MSG = HERE / "文面"
SENDER = HERE / "sender.json"
LOG = OUT / "送信ログ.csv"
PORT = int(os.environ.get("FORMLIST_PORT") or 8780)  # 複数の検索を同時に走らせるとき、MCP が8781以降で別のサーバーを立てる。画面で使うのは8780
assert 8780 <= PORT <= 8790, PORT
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PY = sys.executable[:-len("pythonw.exe")] + "python.exe" if sys.executable.lower().endswith("pythonw.exe") else sys.executable
JOB = {"lines": [], "running": False, "name": ""}
APPROVED = {"key": None}  # 画面で人が「この全文で送ることを承認する」を押した印。AI(MCP)からの本番送信に要る
PREVIEW = {"key": None}  # 本番の前に「対象と全文の確認」を済ませた印。本番を始めたら消す
LOCK = threading.Lock()
_NG_STORE = None
TOKEN = secrets.token_urlsafe(24)  # 起動のたびに変わる。他サイトの画面からの操作を断るためのもので、同じPC上のプロセスは画面を読めば取れる
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
GOOGLE_PLACES_TEXT_BLOCK = "Google Places Text Search is not connected. API terms, billing, and storage/export approval are unverified, so this UI stops before starting a job and does not auto-fallback."


VERSION = hashlib.sha256(b"".join((HERE / f).read_bytes() for f in ("app.py", "autosend.py", "ng_list.py", "site_checker.py"))).hexdigest()[:12]  # 古い版が動き続けていないかをMCPが照合する(読み込み済みの部品も含める)


def run_job(name, args, stdin_text=None, on_ok=None):
    with LOCK:
        if JOB["running"]:
            return False
        JOB.update(lines=[], running=True, name=name, code=None, proc=None)

    def work():
        code = None
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONWARNINGS="ignore")
            p = subprocess.Popen([PY, "-X", "utf8", *args], cwd=HERE, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                 errors="replace", creationflags=NO_WINDOW)
            JOB["proc"] = p
            if stdin_text is not None:
                p.stdin.write(stdin_text)
            p.stdin.close()
            for line in p.stdout:
                JOB["lines"].append(line.rstrip())
            code = p.wait()
            JOB["lines"].append(f"（終了 コード{code}）")
        except Exception as e:
            JOB["lines"].append(f"（処理を始められませんでした: {e!r}）")
        finally:
            with LOCK:  # 確認の印を立てるのも、動いている印を下ろすのも、同じ排他の内側で行う
                try:
                    if code == 0 and on_ok:
                        on_ok()
                finally:
                    JOB.update(running=False, code=code, proc=None)

    threading.Thread(target=work, daemon=True).start()
    return True


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def outputs():
    OUT.mkdir(exist_ok=True)
    return sorted((p.name for p in OUT.glob("フォームリスト_*.csv")), reverse=True)


def messages():
    MSG.mkdir(exist_ok=True)
    if not (MSG / "default.txt").exists():
        src = HERE / "文面ひな形" / "default.txt"
        (MSG / "default.txt").write_text(src.read_text(encoding="utf-8") if src.exists() else "{企業名} ご担当者さま\n", encoding="utf-8")
    return {p.stem: p.read_text(encoding="utf-8-sig") for p in sorted(MSG.glob("*.txt"))}


def fingerprint():
    """文面と送信元の中身。確認したあとに変わったら、確認をやり直させる"""
    h = hashlib.sha256()
    for f in sorted(MSG.glob("*.txt")):
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
        NG_MIGRATION["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return NG_MIGRATION["error"]


def sync_ng_txt(store):
    """テキストのNGに増えた分(送信後に見つかったお断り等)をDBへ取り込む。探す・確認・本番の前に呼ぶ"""
    if not (_NG_SYNC and store is not None):
        return
    try:
        store.sync_legacy_txt(ng_list.FILE)
    except Exception as e:
        NG_MIGRATION["error"] = f"{type(e).__name__}: {str(e)[:200]}"


NGMARK = "画面で「送らない」"


def set_ng_store_for_tests(store):
    """実UIハンドラを一時DBで検証するための注入口。Noneなら従来のテキストNGリストを使う。"""
    global _NG_STORE
    old = _NG_STORE
    _NG_STORE = store
    return old


def add_ng_item(item, reason, company="", store=None):
    if store is None:
        return ng_list.add(item, reason, company) or ng_list.is_ng(item) or ng_list.is_ng_form(item)
    raw = str(item or "").strip()
    if not raw:
        return False
    if raw.startswith("@"):
        url = raw[1:].strip()
        return bool(store.add_form_ng(url, reason, company) or store.is_ng([url]))
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
        return (hp.startswith("http") and ng_list.add(hp, NGMARK, name)) or (form.startswith("http") and ng_list.add_form(form, NGMARK, name))
    try:
        if hp.startswith("http") and store.add_screen_domain_ng(hp, name):
            return True
        if form.startswith("http") and store.add_screen_form_ng(form, name):
            return True
        return (hp and store.is_ng([hp])) or (form and store.is_ng([form]))
    except Exception:
        return False


def ng_undo_screen(line, store=None):
    if store is None:
        return ng_list.remove_line(line)
    return store.undo_screen_ng_line(line) == "ok"


def digest(p, supp, mx, store=None):
    """本番で実際に送る会社と各社の全文。送らないリスト・送信ログ・文面・送信元が変わって送る相手や中身が変われば、値が変わる"""
    import autosend
    sender = json.loads(SENDER.read_text(encoding="utf-8-sig"))
    todo = autosend.targets(str(p), supp, gate=store)[:mx]
    todo, bodies, _rejected = autosend.prepare_message_plan(todo, str(MSG), sender)
    return autosend.plan_digest(todo, bodies, sender)


def count(v, default, lo, hi):
    """空なら既定値。0 や範囲外を既定値に化けさせず、ValueError にする"""
    n = default if v in (None, "") else int(float(v))
    if not lo <= n <= hi:
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
    if re.search(r"[{｛][^}｝]{1,20}[}｝]|【", body):
        return "{…}や【】が残っています。このままでは送られません"
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

    def targets(self, q, ng_store=None):
        """送信対象の一覧と各社の文面(表示用)。送信の可否はここでは決めない(本番は確認ジョブの印が要る)"""
        import autosend
        p = safe_out(q.get("file", ""))
        if not p or not SENDER.exists():
            return self.send(400, {"error": "リストと送信元を先に決めてください"})
        try:
            mx = count(q.get("max"), 20, 1, 200)
            sender = json.loads(SENDER.read_text(encoding="utf-8-sig"))
            todo = autosend.targets(str(p), q.get("supp") == "1", gate=ng_store)[:mx]
            todo, bodies, rejected = autosend.prepare_message_plan(todo, str(MSG), sender)
            rows = [{"name": n, "form": u, "industry": ind, "hp": hp,
                     "subject": autosend.message_adoption.effective_sender(n, u, sender).get("件名", "").replace("{企業名}", n),
                     "warn": _send_warn(body), "notice": _send_notice(body),
                     "text": body} for (n, u, ind, op, hp), body in zip(todo, bodies)]
        except Exception as e:
            return self.send(400, {"error": "対象を読めませんでした。リストのファイルをExcelで開いたままにしていないか確かめて、もう一度「確認する」を押してください", "detail": repr(e)[:200]})
        return self.send(200, rows)

    def do_GET(self):
        u = urlparse(self.path)
        if not self.guard(u.path.startswith("/api/") or u.path == "/download"):
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        ng_store = _NG_STORE
        if u.path == "/":
            return self.send(200, PAGE.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        if u.path == "/api/meta":
            sender = json.loads(SENDER.read_text(encoding="utf-8-sig")) if SENDER.exists() else {}
            return self.send(200, {"industries": list(osm_source.INDUSTRY_TAGS), "sender": sender, "files": outputs(), "messages": messages()})
        if u.path == "/api/job":
            return self.send(200, {"lines": JOB["lines"][-300:], "running": JOB["running"], "name": JOB["name"], "code": JOB.get("code"), "files": outputs(), "version": VERSION, "here": str(HERE)})
        if u.path == "/api/results":
            p = safe_out(q.get("file", ""))
            ng = None if ng_store is not None else ng_list.load()
            rows = [r for r in read_csv(p) if not (r.get("営業お断りの表示") == "あり" or (ng_row_hidden(r, ng_store) if ng is None else ng_list.is_ng_row(r, ng)))] if p else []
            return self.send(200, rows)
        if u.path == "/api/ngscreen":
            return self.send(200, ng_screen(ng_store))
        if u.path == "/api/openings":
            f = safe_out(parse_qs(u.query).get("file", [""])[0])
            if not f or not str(f).endswith(".csv"):
                return self.send(404, {"error": "リストが見つかりません"})
            rows = read_csv(f)
            return self.send(200, {"total": len(rows), "missing": [{"name": r.get("企業名", ""), "industry": r.get("業種", ""), "hp": r.get("HP URL", "")} for r in rows if not (r.get("書き出し") or "").strip()][:200]})
        if u.path == "/api/targets":
            return self.targets(q, ng_store)
        if u.path == "/api/log":
            return self.send(200, read_csv(LOG) if LOG.exists() else [])
        if u.path == "/download":
            p = safe_out(q.get("file", ""))
            if not p:
                return self.send(404, "なし", "text/plain; charset=utf-8")
            return self.send(200, p.read_bytes(), "application/octet-stream",
                             {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(p.name)})
        self.send(404, "なし", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self.guard(True):
            return
        ng_store = _NG_STORE
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send(415, {"error": "json only"})
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            assert isinstance(d, dict)
            lim = count(d.get("limit"), 200, 1, 1000)
            mx = count(d.get("max"), 20, 1, 200)
            par = count(d.get("parallel"), 3, 1, 5)
        except Exception:
            return self.send(400, {"error": "入力の形が正しくありません（数は1以上の整数で入れてください）"})
        if self.path in ("/api/message", "/api/sender", "/api/ng", "/api/ng/undo", "/api/openings") and JOB["running"] and JOB["name"] in ("本番送信", "確認"):
            return self.send(409, {"error": f"「{JOB['name']}」の最中は、文面・送信元・送らない会社を変えられません。終わってから変えてください"})
        if self.path == "/api/search":
            sync_ng_txt(ng_store)
            args = ["formlist.py", "--limit", str(lim), "--parallel", str(par)]
            if ng_store is not None:
                args += ["--ng-state-db", str(ng_store.db_path)]
            if d.get("resume_checks"):
                args.append("--resume-checks")
            if d.get("official_research"):
                args.append("--official-research")
            if d.get("include_large"):
                args.append("--include-large")
            if d.get("portal"):
                args += ["--portal", d["portal"]]
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
            return self.send(200, {"started": run_job("探す", args)})
        if self.path == "/api/quit":
            if JOB["running"] and (JOB["name"] == "本番送信" or d.get("if_idle")):
                return self.send(409, {"error": f"「{JOB['name']}」の最中は終了できません。終わってから押してください"})
            if JOB.get("proc"):
                JOB["proc"].terminate()
            self.send(200, {"ok": True})
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return
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
            if ok or (ng_store is None and ((hp and ng_list.is_ng(hp)) or (form and ng_list.is_ng_form(form)))):
                return self.send(200, {"ok": True})
            return self.send(400, {"error": "この会社は送らないリストに入れられませんでした（ホームページの情報が足りません）"})
        if self.path == "/api/sender":
            old = json.loads(SENDER.read_text(encoding="utf-8-sig")) if SENDER.exists() else {}
            keep = ("会社名", "氏名", "フリガナ", "メール", "電話", "郵便番号", "住所", "件名", "署名", "資料URL")
            bad = [k for k in d if k not in keep]
            if bad:
                return self.send(400, {"error": "送信元にない項目です: " + "、".join(bad[:5])})
            url = str(d.get("資料URL", "")).strip()
            if url and not re.fullmatch(r"https?://[^\s{}【】]+", url):
                return self.send(400, {"error": "資料URLは https:// か http:// で始まる、空白のないURLにしてください（先方にそのまま届きます）"})
            old.update({k: str(v) for k, v in d.items()})  # 画面に出ない項目を消さず、決めた項目だけ文字で受ける
            SENDER.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
            return self.send(200, {"ok": True})
        if self.path == "/api/openings":
            f = safe_out(str(d.get("file", "")))
            ops = d.get("openings")
            if not f or not str(f).endswith(".csv") or not isinstance(ops, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in ops.items()):
                return self.send(400, {"error": "リストのファイル名と、会社名ごとの書き出し（文字）を渡してください"})
            with filelock.locked(f):
                rows = read_csv(f)
                names = {r.get("企業名") for r in rows}
                unknown = [k for k in ops if k not in names]
                if unknown:
                    return self.send(400, {"error": "リストにない会社名です: " + "、".join(unknown[:5])})
                fields = list(rows[0].keys()) if rows else []
                if "書き出し" not in fields:
                    fields.append("書き出し")
                for r in rows:
                    if r.get("企業名") in ops:
                        r["書き出し"] = ops[r["企業名"]].strip()
                buf = io.StringIO(newline="")
                w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\r\n")
                w.writeheader()
                w.writerows(rows)
                filelock.atomic_write(f, buf.getvalue())
            return self.send(200, {"ok": True, "updated": len(ops)})
        if self.path == "/api/approve":
            origin = self.headers.get("Origin") or ""
            if origin.replace("http://", "") not in HOSTS:  # 人が開いた画面(ブラウザ)からの操作だけを受ける。ブラウザは必ず送信元を付ける
                return self.send(403, {"error": "承認は、ツールの画面（ブラウザ）で押してください"})
            if PREVIEW["key"] is None:
                return self.send(400, {"error": "先に「対象の会社と、送る文面の全文を確認する」を済ませてください"})
            APPROVED["key"] = PREVIEW["key"]
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
            bad = [k for k in d if not (k.replace("・", "").replace("_", "").isalnum() or k == "default")]
            if bad or not all(isinstance(v, str) for v in d.values()):
                return self.send(400, {"error": "保存できない名前か中身です: " + "、".join(bad[:5])})
            for k, v in d.items():
                (MSG / f"{k}.txt").write_text(v, encoding="utf-8")
            return self.send(200, {"ok": True})
        if self.path == "/api/send":
            p = safe_out(d.get("file", ""))
            if not p or not SENDER.exists():
                return self.send(400, {"error": "リストと送信元を先に決めてください"})
            args = ["autosend.py", str(p), "--sender", str(SENDER), "--message", str(MSG), "--max", str(mx)]
            if ng_store is not None:
                args += ["--ng-state-db", str(ng_store.db_path)]
            if d.get("supp"):
                args.append("--include-supplemented")
            real = bool(d.get("send"))
            sync_ng_txt(ng_store)
            if real and _NG_SYNC and ng_store is not None and ng_store.migration_status() != "canonical":
                return self.send(409, {"error": "送らない会社の記録の引っ越しが済んでいないため、本番の送信はできません。" + (NG_MIGRATION["error"] or "ツールを起動し直してください")})
            try:
                key = (str(p), p.stat().st_mtime_ns, mx, bool(d.get("supp")), fingerprint(), digest(p, bool(d.get("supp")), mx, ng_store))
            except Exception as e:
                return self.send(400, {"error": "送る対象を数えられませんでした（送信元・リスト・文面を確かめてください）", "detail": repr(e)[:200]})
            if d.get("preview"):
                # 確認の出力に、印と同じ確認番号が出たときだけ印を立てる(見せた全文と印を一致させる)
                return self.send(200, {"started": run_job("確認", args + ["--preview"], on_ok=lambda k=key: (PREVIEW.__setitem__("key", k), APPROVED.__setitem__("key", None)) if f"確認番号: {k[-1]}" in JOB["lines"] else None)})
            if real:
                if d.get("confirm") != "送信":
                    return self.send(400, {"error": "本番は「送信」と入力してください"})
                if PREVIEW["key"] != key:
                    return self.send(400, {"error": "先に「対象の会社と、送る文面の全文を確認する」を押して、全文を読んでから送ってください（リスト・社数・文面・送信元・送らないリスト・送信済みの会社が変わって、送る会社や文面が確認と違うときも、もう一度確認が要ります）"})
                if APPROVED["key"] != key:
                    return self.send(400, {"error": "人の承認がありません。ツールの画面を開き、全文を読んで「この全文で送ることを承認する」を押してから、もう一度送ってください（AIは承認を押せません）"})
                PREVIEW["key"] = None  # 確認は1回の本番ごとに使い切る
                APPROVED["key"] = None
                args += ["--send", "--expect", key[-1]]  # 送る本人が、確認時と同じ会社・全文・送信元かを照合し、違えば送らずに止まる
            return self.send(200, {"started": run_job("本番送信" if real else "試走", args, "送信\n" if real else None)})
        self.send(404, {"error": "なし"})


PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIまるなげ営業</title>
<style>
:root{--bg:#fbf7f5;--card:#fff;--ink:#3a2f2f;--mut:#8a7b7b;--ac:#c4607a;--ac2:#f3dde3;--ok:#2f7d5b;--ng:#b3402f;--line:#e8dcd9;--wb:#fbecc8;--wi:#7a5200}
@media(prefers-color-scheme:dark){:root{--bg:#1e1a1b;--card:#2a2425;--ink:#f1e8e8;--mut:#b5a5a5;--ac:#e58aa3;--ac2:#4a3239;--line:#443a3b;--wb:#4a3d1a;--wi:#f0d58a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.7 "Yu Gothic UI","Meiryo",sans-serif}
header{padding:20px 16px 8px;max-width:980px;margin:auto}h1{margin:0;font-size:22px}header p{margin:4px 0 0;color:var(--mut);font-size:14px}
nav{max-width:980px;margin:auto;padding:0 16px;display:flex;gap:8px;flex-wrap:wrap}
nav button{border:1px solid var(--line);background:var(--card);color:var(--ink);padding:10px 18px;border-radius:999px;font:inherit;cursor:pointer}
nav button.on{background:var(--ac);color:#fff;border-color:var(--ac)}
main{max-width:980px;margin:auto;padding:12px 16px 60px}section{display:none}section.on{display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin:12px 0}
.now{border:1px solid var(--ac);background:var(--ac2);border-radius:12px;padding:12px 16px;margin:12px 0;font-size:15px}.now b{color:var(--ac)}
.now ul{margin:6px 0 0;padding-left:0;list-style:none}.now li{margin:2px 0}
h2{font-size:17px;margin:0 0 10px}label{display:block;font-size:14px;color:var(--mut);margin:10px 0 4px}
input[type=text],input[type=number],textarea,select{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
textarea{min-height:130px}.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.chips{display:flex;flex-wrap:wrap;gap:8px}.chips label{margin:0;display:flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:6px 12px;color:var(--ink);cursor:pointer}
.chips input:checked+span{color:var(--ac);font-weight:700}
.btn{background:var(--ac);color:#fff;border:0;border-radius:10px;padding:12px 22px;font:inherit;cursor:pointer;margin-top:12px}.btn.sub{background:var(--ac2);color:var(--ink)}.btn:disabled{opacity:.5}
.btn.sm{padding:5px 12px;margin:0;font-size:13px;border-radius:8px}
pre{background:#1b1717;color:#e8dcd9;padding:12px;border-radius:10px;max-height:300px;overflow:auto;font-size:13px;white-space:pre-wrap}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}th{color:var(--mut);position:sticky;top:0;background:var(--card)}
.tw{overflow:auto;max-height:560px}table.rt{table-layout:fixed}table.rt td{overflow-wrap:anywhere}
.tag{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;background:var(--ac2)}.tag.ok{background:#d7efe2;color:#1d5a40}.tag.ng{background:#f6d9d3;color:#8a2c1c}.tag.warn{background:var(--wb);color:var(--wi);border:0;border-radius:999px;padding:2px 10px;font-size:12px}
.warn{border-left:4px solid var(--ac);background:var(--ac2);padding:10px 14px;border-radius:8px;font-size:14px}.mut{color:var(--mut);font-size:13px}
.why{font-size:13px;color:var(--mut);margin-top:2px}details.dt summary{cursor:pointer;color:var(--mut);font-size:13px}details.dt div{font-size:13px;color:var(--mut);margin-top:4px}
.sum{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0}.sum span{border:1px solid var(--line);border-radius:10px;padding:4px 12px;font-size:14px}
.st{font-size:15px;margin:0 0 8px}
</style></head><body>
<header><button type="button" id="quit" style="float:right;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:6px 14px;border-radius:999px;font:inherit;cursor:pointer">ツールを終了する</button><h1>AIまるなげ営業</h1><p>地域と業種から、問い合わせフォームのある会社を探して、リストにします。</p></header>
<nav><button data-t="a" class="on">1 探す</button><button data-t="b">2 結果を見る</button><button data-t="c">3 送る</button><button data-t="d">送信の記録</button></nav>
<main>
<section id="a" class="on">
 <div class="now" id="now_a"></div>
 <div class="card"><h2>どこで、何を探しますか</h2>
  <div class="row" style="margin-top:8px"><div><label>探す場所（市区町村まで）</label><input id="area" type="text" placeholder="例 東京都江東区"></div>
  <div><label>調べる会社の上限（多いほど時間がかかります）</label><input id="limit" type="number" value="300" min="1"><div class="mut" id="est"></div></div></div>
  <label style="display:flex;gap:6px;align-items:center;color:var(--ink)"><input type="checkbox" id="large">大手・チェーンの会社も調べる（ふつうは外します。一人サロンや小さな会社を探すときはチェックしないでください）</label>
  <label>業種（いくつでも）</label><div class="chips" id="inds"></div><p class="mut">迷ったら、戦略書の優先順位どおり「商業施設 → 住宅・不動産 → ホテル・旅館 → ブライダル → 百貨店」の上から、1〜2業種ずつ探すと、結果を読みやすくなります。</p>
  <label>キーワード（店名・施設名に含まれる語。スペースか「、」で区切ると、いくつでも）</label><input id="kw" type="text" placeholder="例 サロン、フラワー、ケアハウス">
  <p class="mut">業種を選ばず、キーワードだけでも探せます。地図に載っている店名・施設名から探すので、事業内容では探せません。業種とキーワードを両方入れると、両方で探します。キーワードをいくつか入れると、最大3つずつ同時に探します（同時に探しすぎるとGoogleマップに止められやすいため、3つまでにしています）。</p>
  <details style="margin-top:14px"><summary>くわしい設定（最初は開かなくて大丈夫です）</summary>
   <label>探す先（いくつでも）</label>
   <div id="srcs" class="chips"><label><input type="checkbox" value="google" checked> Googleマップ（件数を絞り、ゆっくり集めます。ホームページの欄がある店が多い）</label> <label><input type="checkbox" value="osm"> 地図データ（OpenStreetMap。ホームページの載っている店は少ない）</label> <label title="Google Places Text Search is not connected"><input type="checkbox" value="google_places_text" disabled> Google Places Text Search (not connected)</label></div>
   <p class="mut">Googleマップは規約上、自動での取得を認めていません。1回の検索で20件までに絞り、1件ごとに間隔を空けます。ブロックされたら止まります。</p>
   <label>ポータルサイトのURLから集める（上の場所と業種の代わり）</label><input id="portal" type="text" placeholder="https://...（Indeed・業界ポータルなどの、絞り込み後の一覧ページ）">
   <label>送らない会社を先に入れる（既存のお客さま・競合のホームページのドメインを、1行に1つ）</label><textarea id="ng" placeholder="example.com"></textarea>
   <p class="mut">探したあとで「送らない」を押して入れることもできます。</p></details>
  <button class="btn" id="go">探しはじめる</button></div>
 <div class="card"><h2>進み具合</h2><p class="st" id="st_a"></p><details><summary class="mut">くわしい経過（黒い画面）を見る</summary><pre id="log">まだ何も動いていません</pre></details></div>
</section>
<section id="b"><div class="now" id="now_b"></div><div class="card"><h2>できたリスト</h2>
  <select id="files"></select> <label style="display:inline-flex;gap:6px;margin:0 8px"><input type="checkbox" id="onlyOk" checked>問い合わせ候補だけ表示</label><div style="margin-top:8px"><button class="btn sub sm" id="dl">Excel用にCSVをダウンロード</button></div>
 <div class="sum" id="sum"></div>
 <div class="tw" style="margin-top:8px"><table class="rt" id="res"></table></div>
 <details class="dt" style="margin-top:12px"><summary>送らない会社に入れたもの（ここから戻せます）</summary><div id="nglist"></div></details></div></section>
<section id="c">
 <div class="now" id="now_c"></div>
 <div class="card"><h2>準備1　送信元（先方に表示される名前と連絡先）</h2><div class="row" id="sender"></div><p class="mut">「必須」の欄は、空だと送れません。フリガナ・電話・住所などは、フォームに同じ欄があるときだけ使います。</p><button class="btn sub" id="saveSender">保存</button></div>
 <div class="card"><h2>準備2　文面</h2><p class="mut">{企業名} と書いた所は、送るとき相手の会社名に自動で置き換わります。{書き出し} は会社ごとの書き出しの一文に（無い会社は消えます）、{署名} は上の署名に置き換わります。この3つの記号はそのまま残してください。それ以外の文章は、直して保存して大丈夫です。業種ごとの文面があれば、その業種の会社にはそちらを使います。送信者の名前・連絡先・配信停止の案内は消さないでください（特定電子メール法）。</p>
  <label>業種を選ぶ（「標準」はどの業種にも使われます）</label><select id="mind"></select>
  <details class="dt"><summary>営業資料から、文面のたたき台を作る</summary><p class="why">営業資料の文章を貼ってください。見出し・箇条書き・URLを拾って、下の文面欄にたたき台を入れます。保存するまでは、文面は変わりません。AIは使っていません。必ず自分で読んで直してください。</p><textarea id="mat" style="min-height:120px" placeholder="営業資料の文章をここに貼り付け"></textarea><button class="btn sub" id="mkSample" type="button">たたき台を作って、下に入れる</button></details>
  <textarea id="mtext" style="min-height:260px"></textarea><button class="btn sub" id="saveMsg">この文面を保存</button></div>
 <div class="card"><h2>練習 → 確認 → 本番</h2>
  <div class="row"><div><label>送るリスト</label><select id="sfile"></select></div><div><label>今回送る最大の社数（最初は3社くらいが安心です）</label><input id="smax" type="number" value="3" min="1"></div></div>
  <label style="display:flex;gap:6px;align-items:center;color:var(--ink)"><input type="checkbox" id="supp">検索で見つけたホームページの会社にも送る（別の会社のページを拾っている恐れがあります。結果の画面で「確認が要る」の会社を見てから）</label>
  <p class="warn">対象は、問い合わせフォーム候補が検出され、営業お断りの表示がなく、まだ処理していない会社だけです。フォーム検出は営業送信の許可ではありません。自動入力できない確認画面（画像認証など）や、選ぶ形式の必須項目があるフォームは送らず、「手で送る必要あり」と記録します。</p>
  <button class="btn sub" id="dry">練習する（フォームに入力して画面を保存するだけ。送りません）</button>
  <hr style="border:0;border-top:1px solid var(--line);margin:20px 0">
  <button class="btn" id="prev">対象の会社と、送る文面の全文を確認する（本番の前に必ず押します）</button>
  <label>本番: 下の一覧と文面を読んで、よければ「送信」と入力して下のボタンを押すと送ります（毎回入力が必要です）</label><input id="confirm" type="text" placeholder="送信"><label>AI(ClaudeやCodex)に送らせるときは、全文を読んだうえで、このボタンを押してください。押すまでAIは送れません</label><button class="btn sub" id="approve" type="button">この全文で送ることを承認する</button>
  <button class="btn" id="real" style="background:var(--ng)">本番で送る</button></div>
 <div class="card"><h2>送る対象と文面（確認ボタンで出ます）</h2><div id="tg"><p class="mut">まだ確認していません。上の「対象の会社と、送る文面を確認する」を押してください。</p></div></div>
 <div class="card"><h2>進み具合</h2><p class="st" id="st_c"></p><details><summary class="mut">くわしい経過（黒い画面）を見る</summary><pre id="log2">まだ何も動いていません</pre></details></div>
</section>
<section id="d"><div class="now" id="now_d"></div><div class="card"><h2>送信の記録</h2><button class="btn sub sm" id="reload">更新</button><div class="sum" id="hsum"></div><div class="tw" style="margin-top:8px"><table class="rt" id="hist"></table></div></div></section>
</main>
<script>
const TOKEN='__TOKEN__';const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const J=(u,o={})=>fetch(u,{...o,headers:{...(o.headers||{}),'X-Token':TOKEN}}).then(async r=>{const d=await r.json().catch(()=>({error:'サーバーから読み取れない応答（'+r.status+'）'}));if(r.status==403){alert(d.error||'拒否されました');if(d.code=='token')location.reload();return {error:d.error}}return d});
const post=(u,d)=>J(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
let META={},JOB={lines:[],running:false,name:'',code:null,files:[]},RESROWS=[];
const SF=['会社名','氏名','フリガナ','メール','電話','郵便番号','住所','件名','資料URL'],NEED=['会社名','氏名','メール','署名'];
function goto(t){document.querySelector('nav button[data-t="'+t+'"]').click()}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button,section').forEach(e=>e.classList.remove('on'));b.classList.add('on');$('#'+b.dataset.t).classList.add('on');if(b.dataset.t=='d')hist();if(b.dataset.t=='b')loadRes();banners()});
function opts(sel,list,cur){sel.innerHTML=list.map(f=>`<option ${f==cur?'selected':''}>${esc(f)}</option>`).join('')}
function tag(t,k){return `<span class="tag ${k||''}">${esc(t)}</span>`}
function lk(v,label){try{const u=new URL(v);if(u.protocol=='http:'||u.protocol=='https:')return `<a href="${esc(u.href)}" target="_blank" rel="noopener noreferrer" title="${esc(u.hostname)}">${esc(label)}</a>`}catch(e){}return ''}
function plain(m){m=String(m||'');
 if(/Timeout/i.test(m))return 'ページが開くのに時間がかかりすぎて、時間切れになりました。時間を置くと確認できることがあります';
 if(/captcha/i.test(m))return '自動入力できない確認画面（画像認証など）があります。手で送る必要があります';
 if(/ConnectionError|ERR_|Max retries|NameResolution/i.test(m))return '接続できませんでした（相手のサイトが止まっているか、通信が不安定です）';
 if(/本文欄が見つからない/.test(m))return '文面を入れる欄が見つかりませんでした。手で送る必要があります';
 if(/送信ボタンが見つからない/.test(m))return '送信ボタンが見つかりませんでした。手で送る必要があります';
 if(/完了画面の文言を確認できず/.test(m))return '送信ボタンは押しましたが、送れたかを画面で確認できませんでした。そのフォームを開いて確かめてください';
 if(/差し込みの取り残し/.test(m))return '文面に {…} や 【】 が残っているため送りませんでした。文面を見直してください';
 return m.replace(/^入力: ?/,'入力できた項目: ').replace('（送信ボタンは押していない）','（練習なので送信ボタンは押していません）').replace(/^error: ?/,'読み込みに失敗: ')}
function verdict(r){const v=r['フォーム判定']||'',sup=(r['取得元']||'').includes('補完');
 if(v=='フォームあり')return sup?{k:'warn',t:'要確認',why:'ホームページを検索で見つけた会社です。'+(r['HPの照合']?'照合の結果は「'+r['HPの照合']+'」。':'')+'別の会社のページでないかを開いて確かめてください。営業送信可否は未確認です'}:{k:'ok',t:'フォーム検出',why:'問い合わせフォーム候補が見つかりました。営業送信可否は未確認です'};
 if(v=='要目視')return {k:'warn',t:'手で確かめる',why:'このツールでは送りません。ホームページを開いて、問い合わせフォームがあれば、自分で文面を貼って送ります'};
 if(v=='メールのみ')return {k:'ng',t:'対象外',why:'メールアドレスだけ見つかりました（このツールはフォーム候補の確認を優先します）'};
 if(v=='見つからず')return {k:'ng',t:'対象外',why:'問い合わせ候補を確認できませんでした'};
 if(v=='robots禁止')return {k:'ng',t:'対象外',why:'ホームページ側が、自動で読むことを断っています'};
 if(v=='取得失敗')return {k:'ng',t:'対象外',why:'ホームページを読み込めませんでした（時間を置くと読めることがあります）'};
 return {k:'',t:v||'未判定',why:''}}
function senderMissing(){const s=META.sender||{};const m=NEED.filter(k=>!(s[k]||'').trim()||(s[k]||'').includes('【'));if(!m.includes('署名')&&!/@|\d{2,4}-\d{2,4}-\d{3,4}/.test(s['署名']||''))m.push('署名に連絡先（メールか電話）');return m}
function est(){const n=Math.max(1,parseInt($('#limit').value)||0);const a=Math.max(1,Math.round((n*3+30)/60)),b=Math.max(1,Math.round((n*15+60)/60));$('#est').textContent=`最大${n}社まで調べます。目安は約${a}〜${b}分です（実測の一例: ホテル・旅館10社で約40秒。会社数や通信で変わります）`}
$('#limit').oninput=est;
function banners(){const j=JOB,mine=j.name=='探す';
 const run=(t)=>`<b>いま:</b> ${esc(t)}を実行しています。このままお待ちください。`;
 $('#now_a').innerHTML=j.running&&mine?run('探す処理')+'<br>終わると自動で「2 結果を見る」に移ります。':META.files&&META.files.length?'<b>いま:</b> 探す前の画面です。前に作ったリストは「2 結果を見る」で見られます。<br><b>次:</b> 新しく探すなら、場所と業種を入れて「探しはじめる」を押します。':'<b>いま:</b> 最初の画面です。<br><b>次:</b> 場所と業種を入れて「探しはじめる」を押します。迷ったら「例を入れる」を押してください。';
 const s=RESROWS.length?summ(RESROWS):null;
 $('#now_b').innerHTML=s?`<b>いま:</b> 探した結果です。<br><b>次:</b> 「フォーム検出」${s.ok}社は、問い合わせフォーム候補が見つかった会社です。営業送信可否は未確認なので、必要に応じてホームページを開いて確認してください。「要確認」の会社も、ホームページを開いて見てください。既存のお客さまや、対象にしない会社は「送らない」を押します。よければ「3 送る」へ進みます。 <button class="btn sm" id="go3" type="button">3 送るへ進む</button> ${s.ok>1?`<button class="btn sub sm" id="goall" type="button">「フォーム検出」${Math.min(s.ok,200)}社を確認へ進める</button>`:''}`:'<b>いま:</b> 結果の画面です。リストがまだありません。<br><b>次:</b> 「1 探す」で探してください。';
 const g=$('#go3');if(g)g.onclick=()=>goto('c');const ga=$('#goall');if(ga)ga.onclick=()=>{$('#sfile').value=$('#files').value;$('#smax').value=Math.min(s.ok,200);$('#smax').dispatchEvent(new Event('input'));goto('c')};
 const m=senderMissing();
 const li=[m.length?`<li>× 準備1　送信元が足りません: ${esc(m.join('、'))}</li>`:'<li>○ 準備1　送信元が入っています（保存済み）</li>','<li>・ 準備2　文面を読んで、直すところがあれば保存します</li>','<li>・ 練習　「練習する」で、フォームに入力だけ試せます（送りません）。最初は1回おすすめです</li>',`<li>${STATE.prevOk?'○':'・'} 確認　対象の会社と文面の確認${STATE.prevOk?'が終わりました。読んで良ければ「送信」と入力して本番へ':'を押します（本番の前に必ず）'}</li>`];
 $('#now_c').innerHTML=(j.running&&!mine?run(j.name)+'<br>':'')+'<b>次にやること（上から順に）</b><ul>'+li.join('')+'</ul>';
 $('#now_d').innerHTML='<b>いま:</b> 練習と本番の記録です（練習の記録も入っています）。<br><b>手で送る必要あり:</b> フォームを開き、「3 送る」の文面をコピーして貼り、自分で送信します。<br><b>送れたか確認が要る:</b> 送信ボタンは押しています。相手から自動返信のメールが届いていれば送れています。届かなくても、送れていないとは限りません(自動返信が無い会社や、迷惑メールに入る場合があります)。送り直す前に、送信後の画面の保存(出力/送信画面)を見て、送れたかを確かめてください。'}
const legacyVerdict=verdict;verdict=function(r){const cc=r['contact_confidence']||'',sp=r['sales_permission']||'';if(sp&&sp!='未確認')return {k:'ng',t:'営業不可候補',why:sp+'。フォームの有無とは別に、人が確認するまで送信許可とは扱いません'};if(cc=='公式HP再探索待ち')return {k:'warn',t:'公式HP再探索待ち',why:'SNS/予約ポータル等のURLです。公式HP再探索はまだ未実施です。この画面では送信対象にしません'};if(cc=='自動確認:フォーム')return {k:'ok',t:'自動確認:フォーム',why:'問い合わせフォーム候補を自動確認しました。営業送信可否は未確認です'};if(cc=='自動確認:メール')return {k:'warn',t:'自動確認:メール',why:'メール先は見つかりました。フォーム送信ではなく、営業可否は未確認です'};if(cc=='候補:要目視')return {k:'warn',t:'候補:要目視',why:'問い合わせ候補はありますが、自動確認できていません。営業可否も未確認です'};if(cc=='未発見')return {k:'ng',t:'未発見',why:'問い合わせ候補を確認できませんでした'};return legacyVerdict(r)}
const officialVerdict=verdict;verdict=function(r){const cc=r['contact_confidence']||'',d=r['source_detail']||'';if(cc=='公式HP要目視')return {k:'warn',t:'公式HP要目視',why:'公式HPらしい候補はありますが、電話または住所の一致が足りないため元URLのままです。候補URLは詳細を確認してください'};if(cc=='公式HP再探索済み:候補なし')return {k:'ng',t:'公式HP候補なし',why:'公式HP再探索を試しましたが、採用できる候補は見つかりませんでした'};if(cc=='公式HP再探索失敗')return {k:'ng',t:'公式HP再探索失敗',why:'公式HP再探索中に通信または確認エラーがありました。迂回取得はしていません'};if(cc=='公式HP再探索無効')return {k:'ng',t:'公式HP再探索無効',why:'見つかった候補がNGまたは拒否基準に該当したため対象外にしました'};if(cc=='公式HP再探索済み:候補なし'&&/official_site_research:from=/.test(d))return {k:'warn',t:'公式HP再探索未採用',why:'公式HP再探索は実施されましたが、採用できる候補がまだありません'};return officialVerdict(r)}
const STATE={prevOk:false};let TGN=0;
function unprev(){if(!STATE.prevOk)return;STATE.prevOk=false;TGN=0;$('#tg').innerHTML='<p class="warn">リスト・社数・文面・送信元のどれかが変わりました。もう一度「対象の会社と、送る文面の全文を確認する」を押してください。</p>';banners()}
function summ(rows){const c={ok:0,warn:0,ng:0,auto:0,cand:0,missing:0,official:0};rows.forEach(r=>{const k=verdict(r).k;if(k in c)c[k]++;const cc=r['contact_confidence']||'';if(cc=='自動確認:フォーム'||cc=='自動確認:メール')c.auto++;if(r['contact_candidate']=='True'||r['contact_candidate']===true||['自動確認:フォーム','自動確認:メール','候補:要目視'].includes(cc))c.cand++;if(cc=='未発見')c.missing++;if(cc=='公式HP再探索待ち')c.official++});return c}
async function init(){META=await J('/api/meta');
 const rb=document.createElement('label');rb.style.cssText='display:flex;gap:6px;align-items:center;color:var(--ink);margin-top:10px';rb.innerHTML='<input type="checkbox" id="resumeChecks" checked> 途中で止まっても確認済みの行を再利用する';$('#go').before(rb);
 const ob=document.createElement('label');ob.style.cssText='display:flex;gap:6px;align-items:center;color:var(--ink);margin-top:10px';ob.innerHTML='<input type="checkbox" id="officialResearch" checked> SNSやポータルサイトしか見つからない会社は、公式ホームページを探し直して、連絡先を確かめ直す';$('#go').before(ob);
 $('#inds').innerHTML=META.industries.map(i=>`<label><input type="checkbox" value="${esc(i)}"><span>${esc(i)}</span></label>`).join('');
 $('#sender').innerHTML=SF.map(k=>`<div><label>${k}${NEED.includes(k)?'（必須）':''}</label><input type="text" id="s_${k}" value="${esc(META.sender[k])}"></div>`).join('')+`<div style="grid-column:1/-1"><label>署名（必須・文面の {署名} に入ります。連絡先と、配信停止の案内を含めてください。例: 今後このようなご案内が不要でしたら、メールアドレス宛てに「不要」とご返信ください。以後お送りしません。）</label><textarea id="s_署名" style="min-height:110px">${esc(META.sender['署名'])}</textarea></div>`;
 opts($('#files'),META.files);opts($('#sfile'),META.files);
 const names=['default',...Object.keys(META.messages).filter(k=>k!='default')].concat(META.industries.filter(i=>!(i in META.messages)));
 $('#mind').innerHTML=names.map(n=>`<option value="${esc(n)}">${esc(n=='default'?'標準':n)}${n in META.messages?'':'（まだ無い）'}</option>`).join('');
 showMsg();est();banners()}
function showMsg(){const k=$('#mind').value;$('#mtext').value=META.messages[k]??META.messages.default}
$('#mind').onchange=showMsg;
['#sfile','#smax','#supp'].forEach(x=>{$(x).addEventListener('change',()=>unprev());$(x).addEventListener('input',()=>unprev())});
$('#approve').onclick=async()=>{const r=await post('/api/approve',{});if(r.error)return alert(r.error);alert('承認しました。AIに「送って」と伝えてください。承認は1回の送信で使い切ります')};
$('#mkSample').onclick=async()=>{const r=await post('/api/sample',{material:$('#mat').value});if(r.error)return alert(r.error);if($('#mtext').value.trim()&&!confirm('いまの文面欄を、たたき台で置き換えます（保存するまでは元に戻せます）。よろしいですか'))return;$('#mtext').value=r.text;alert('たたき台を入れました。拾えたもの: 題「'+(r.title||'なし')+'」・箇条書き'+r.points.length+'個・URL'+(r.url?'あり':'なし')+'。読んで直してから「この文面を保存」を押してください')};
$('#saveMsg').onclick=async()=>{const k=$('#mind').value;META.messages[k]=$('#mtext').value;const r=await post('/api/message',{[k]:$('#mtext').value});if(r.error)return alert(r.error);unprev();alert('保存しました')};
$('#saveSender').onclick=async()=>{const d={};[...SF,'署名'].forEach(k=>d[k]=$('#s_'+k).value);const r=await post('/api/sender',d);if(r.error)return alert(r.error);META.sender={...META.sender,...d};unprev();banners();alert('保存しました')};
$('#go').onclick=async()=>{const r=await post('/api/search',{area:$('#area').value,limit:$('#limit').value,include_large:$('#large').checked,resume_checks:!$('#resumeChecks')||$('#resumeChecks').checked,official_research:!$('#officialResearch')||$('#officialResearch').checked,portal:$('#portal').value,ng:$('#ng').value,keywords:$('#kw').value,sources:[...document.querySelectorAll('#srcs input:checked')].map(x=>x.value),industries:[...document.querySelectorAll('#inds input:checked')].map(x=>x.value)});
 if(r.error)return alert(r.error);if(!r.started)alert('ほかの処理が動いています。終わるまでお待ちください');else wasRun=true;poll()};
async function send(real,preview){const d={file:$('#sfile').value,max:$('#smax').value,send:real,preview:!!preview,confirm:$('#confirm').value,supp:$('#supp').checked};
 if(!d.file)return alert('先にリストを作ってください');
 if(!preview){STATE.prevOk=false}
 if(real){const ap=await post('/api/approve',{});if(ap.error)return alert(ap.error)}
 if(real&&!confirm('本番で '+TGN+' 社に送ります。送信元は「'+(META.sender['会社名']||'')+' '+(META.sender['氏名']||'')+'」です。一覧の文面を開いて読みましたか？送り始めたら、途中で文面は変えられません。'))return;
 const r=await post('/api/send',d);if(real)$('#confirm').value='';if(r.error)return alert(r.error);if(!r.started)alert('ほかの処理が動いています');else wasRun=true;poll()}
$('#dry').onclick=()=>send(false);$('#real').onclick=()=>send(true);$('#prev').onclick=()=>{STATE.prevOk=false;send(false,true)};
let timer,wasRun=false;
function paint(id,own){const j=JOB;$(id).textContent=own&&(j.lines.length||j.running)?(j.running?'【'+j.name+'を実行中】\n':'')+j.lines.join('\n'):'まだ何も動いていません';$(id).scrollTop=1e9}
function status(){const j=JOB,mine=j.name=='探す',last=(j.lines.filter(l=>l.trim()).slice(-1)[0]||'');
 const f=(own)=>!own||!j.name?'まだ何も動いていません':j.running?`【${j.name}】実行中… ${last}`:j.code===0?`【${j.name}】終わりました。`:`【${j.name}】途中で止まりました。くわしい経過の最後の行を見てください。`;
 $('#st_a').textContent=f(mine);$('#st_c').textContent=f(!mine)}
async function poll(){clearInterval(timer);const t=async()=>{const j=await J('/api/job');if(j.error)return;JOB=j;const mine=j.name=='探す';paint('#log',mine);paint('#log2',!mine);status();banners();
 if(j.running)wasRun=true;
 if(!j.running){clearInterval(timer);const fin=wasRun&&mine&&j.code===0;
  if(wasRun&&j.code!==0&&j.code!==null)alert('「'+j.name+'」が途中で止まりました。進み具合のくわしい経過に、理由が出ています');
  opts($('#files'),j.files,fin?j.files[0]:$('#files').value);opts($('#sfile'),j.files,fin?j.files[0]:$('#sfile').value);META.files=j.files;
  if(wasRun&&j.name=='確認'&&j.code===0){STATE.prevOk=true;loadTargets()}
  if(wasRun&&j.name=='本番送信'&&j.code===0)goto('d');
  wasRun=false;banners();if(fin)goto('b')}};t();timer=setInterval(t,1500)}
async function loadTargets(){const r=await J('/api/targets?file='+encodeURIComponent($('#sfile').value)+'&max='+encodeURIComponent($('#smax').value)+'&supp='+($('#supp').checked?1:0));
 if(r.error){$('#tg').innerHTML=`<p class="warn">${esc(r.error)}</p>`;return}
 TGN=r.length;if(!r.length){$('#tg').innerHTML='<p>送る対象の会社はありません（すでに送った・お断り・「送らない」に入れた会社だけでした）。</p>';return}
 $('#tg').innerHTML=`<p>次の ${r.length} 社に、それぞれの会社名を入れた文面が送られます。送信元は「${esc(META.sender['会社名'])} ${esc(META.sender['氏名'])} ${esc(META.sender['メール'])}」です。文面をすべて読んでから、本番を押してください。</p><div class="tw"><table class="rt"><tr><th style="width:30%">会社</th><th style="width:20%">業種</th><th style="width:18%">フォーム</th><th>文面</th></tr>`+r.map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(x.industry||'指定なし')}</td><td>${lk(x.form,'フォームを開く')}</td><td>${x.warn?'<div style="color:#b00020;font-weight:700">'+esc(x.warn)+'</div>':''}${x.notice?'<div style="color:#8a5a00">'+esc(x.notice)+'</div>':''}<details class="dt"${r.length<=5?' open':''}><summary>文面を見る</summary><div style="white-space:pre-wrap">${esc("件名: "+x.subject+"\n\n"+x.text)}</div></details></td></tr>`).join('')+'</table></div>'}
async function loadRes(){const f=$('#files').value;if(!f){$('#res').innerHTML='<tr><td>まだリストがありません</td></tr>';RESROWS=[];$('#sum').innerHTML='';banners();return}
 const all=await J('/api/results?file='+encodeURIComponent(f));if(all.error)return;RESROWS=all;const s=summ(all);
 $('#sum').innerHTML=`<span>全${all.length}社</span><span>${tag('フォーム検出','ok')} ${s.ok}社</span><span>${tag('要確認','warn')} ${s.warn}社</span><span>${tag('対象外','ng')} ${s.ng}社${$('#onlyOk').checked?'（今は表示していません）':''}</span>`;
 $('#sum').innerHTML+=`<span>自動確認 ${s.auto}件</span><span>候補込み ${s.cand}件</span><span>未発見 ${s.missing}件</span><span>公式HP再探索待ち ${s.official}件</span>`;
 const rank={ok:0,warn:1,ng:2,'':3};let rows=all.map(r=>({r,v:verdict(r)}));if($('#onlyOk').checked)rows=rows.filter(x=>x.v.k=='ok'||x.v.k=='warn');rows.sort((a,b)=>rank[a.v.k]-rank[b.v.k]);
 $('#res').innerHTML='<tr><th style="width:27%">会社</th><th style="width:38%">検出状態</th><th style="width:20%">開いて確かめる</th><th style="width:15%"></th></tr>'+(rows.length?rows.map(({r,v},i)=>{const form=r['問い合わせフォームURL 無ければメール']||'',mail=r['メールアドレス']||(form.startsWith('http')?'':form);
  return `<tr><td>${esc(r['企業名'])}<div class="why">${esc(r['業種'])}</div><details class="dt"><summary>くわしく</summary><div>取得元: ${esc(r['取得元'])}<br>メール: ${esc(mail||'なし')}<br>メモ: ${esc(plain(r['判定メモ']))||'なし'}</div></details></td><td>${tag(v.t,v.k)}<div class="why">${esc(v.why)}</div></td><td>${lk(r['HP URL'],'ホームページを開く')}<br>${lk(form,'フォームを開く')}</td><td><button class="btn sub sm ngb" data-i="${i}" type="button">送らない</button></td></tr>`}).join(''):'<tr><td colspan="4">表示できる会社がありません。「問い合わせ候補だけ表示」を外すと、対象外の会社も見えます。</td></tr>');
 document.querySelectorAll('.ngb').forEach(b=>b.onclick=async()=>{const x=rows[+b.dataset.i].r;const form=x['問い合わせフォームURL 無ければメール']||'';
  const hostOf=v=>{try{return new URL(v).hostname.replace(/^www\./,'')}catch(e){return ''}};const host=hostOf(x['HP URL']||'');
  const same=host?RESROWS.filter(y=>y!==x&&hostOf(y['HP URL']||'')==host).map(y=>y['企業名']):[];
  if(!confirm('「'+x['企業名']+'」を送らない会社に入れます。'+(host?'ホームページ（'+host+'）ごと入れるので、':'')+(same.length?'同じホームページの '+same.length+' 社（'+same.slice(0,3).join('、')+(same.length>3?' ほか':'')+'）も送らない会社になります。':'')+'この画面の下の「送らない会社に入れたもの」から、あとで戻せます。よろしいですか'))return;
  const res=await post('/api/ng',{hp:x['HP URL']||'',form:form,name:x['企業名']});if(res.error)return alert(res.error);loadRes()});
 ngList();banners()}
async function ngList(){const r=await J('/api/ngscreen');if(r.error)return;$('#nglist').innerHTML=r.length?r.map((l,i)=>`<div style="margin:4px 0">${esc(l.split('#')[0].trim().replace(/^@/,''))} <span class="why">${esc(l.split('「送らない」')[1]||'')}</span> <button class="btn sub sm ngu" data-i="${i}" type="button">戻す</button></div>`).join(''):'<div class="why">まだありません</div>';document.querySelectorAll('.ngu').forEach(b=>b.onclick=async()=>{const res=await post('/api/ng/undo',{line:r[+b.dataset.i]});if(res.error)return alert(res.error);loadRes()})}
$('#files').onchange=loadRes;$('#onlyOk').onchange=loadRes;$('#dl').onclick=async()=>{const f=$('#files').value;if(!f)return;const r=await fetch('/download?file='+encodeURIComponent(f),{headers:{'X-Token':TOKEN}});const a=document.createElement('a');a.href=URL.createObjectURL(await r.blob());a.download=f;a.click()};
const RES={'試走OK':['練習OK（送信ボタンは押していません）','ok'],'送信済み':['送信済み','ok'],'要手動':['手で送る必要あり','warn'],'不明(要確認)':['送れたか確認が要る','warn'],'入力エラー(要確認)':['入力エラーの表示あり（届いていない可能性が高い。自動では再送しません）','warn'],'送信試行':['送信ボタンを押した（結果が未確認）','warn'],'お断り表示(送らない)':['お断りの表示があり、送らない','ng']};
async function hist(){const rows=await J('/api/log');if(rows.error)return;const cnt={};rows.forEach(r=>{const n=(RES[r['結果']]||[r['結果']])[0];cnt[n]=(cnt[n]||0)+1});
 $('#hsum').innerHTML=Object.entries(cnt).map(([k,v])=>`<span>${esc(k)} ${v}件</span>`).join('');
 $('#hist').innerHTML=rows.length?'<tr><th style="width:15%">日時</th><th style="width:22%">会社</th><th style="width:20%">結果</th><th>わけ・次にすること</th><th style="width:14%"></th></tr>'+rows.slice().reverse().map(r=>{const m=RES[r['結果']]||[r['結果'],''];return `<tr><td>${esc((r['日時']||'').slice(5))}</td><td>${esc(r['企業名'])}</td><td>${tag(m[0],m[1])}</td><td>${esc(plain(r['メモ']))}</td><td>${lk(r['フォームURL'],'フォームを開く')}</td></tr>`}).join(''):'<tr><td>まだ記録がありません</td></tr>'}
$('#reload').onclick=hist;
$('#quit').onclick=async()=>{if(!confirm('ツールを終了します。送信などの処理が動いている場合は止まります。よろしいですか'))return;try{await post('/api/quit',{})}catch(e){}document.body.innerHTML='<p style="padding:40px">終了しました。このタブは閉じてください。再び使うときは、デスクトップの「AIまるなげ営業」を押します。</p>'};
init();poll();
</script></body></html>"""


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    import socket
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            if not os.environ.get("FORMLIST_NO_BROWSER"):  # MCP から起動したときは画面を開かない
                webbrowser.open(f"http://127.0.0.1:{PORT}/")  # すでに動いている。新しく起動せず、画面だけ開く
            sys.exit(0)
    init_ng_store()
    _NG_SYNC = True
    ensure_ng_migrated(_NG_STORE)
    if NG_MIGRATION["error"]:
        print("送らない会社の引っ越しに失敗しました。本番の送信は止まります:", NG_MIGRATION["error"])
    srv = Srv(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"ブラウザで {url} を開いてください。終わるときはこの黒い画面を閉じます。")
    if not os.environ.get("FORMLIST_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


