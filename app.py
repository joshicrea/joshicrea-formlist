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
import approval
import message_adoption
import sales_records
import send_rules
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
PREVIEW = {"key": None, "names": []}  # 本番の前に「対象と全文の確認」を済ませた印と、その確認に出た会社名。本番を始めたら消す
LOCK = threading.Lock()
_NG_STORE = None
TOKEN = secrets.token_urlsafe(24)  # 起動のたびに変わる。他サイトの画面からの操作を断るためのもので、同じPC上のプロセスは画面を読めば取れる
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
GOOGLE_PLACES_TEXT_BLOCK = "Google Places Text Search is not connected. API terms, billing, and storage/export approval are unverified, so this UI stops before starting a job and does not auto-fallback."


VERSION = hashlib.sha256(b"".join((HERE / f).read_bytes() for f in ("app.py", "autosend.py", "ng_list.py", "site_checker.py"))).hexdigest()[:12]  # 古い版が動き続けていないかをMCPが照合する(読み込み済みの部品も含める)


def run_job(name, args, stdin_text=None, on_ok=None, extra_env=None):
    with LOCK:
        if JOB["running"]:
            return False
        JOB.update(lines=[], running=True, name=name, code=None, proc=None, started=time.strftime("%Y-%m-%dT%H:%M:%S"))

    def work():
        code = None
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONWARNINGS="ignore", **(extra_env or {}))
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


def preview_names(lines):
    """確認(--preview)の出力に出た、送る会社の名前。承認ダイアログに出す"""
    out = []
    for line in lines:
        m = re.match(r"===== 送る文面 (.+?)（業種", line)
        if m:
            out.append(m.group(1))
    return out


def set_preview(key):
    PREVIEW["key"] = key
    PREVIEW["names"] = preview_names(JOB["lines"])
    APPROVED["key"] = None


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def outputs():
    OUT.mkdir(exist_ok=True)
    files = [p for p in OUT.glob("フォームリスト_*.csv") if p.stat().st_size > 0]
    return [p.name for p in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)]


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

    def manual_kit(self, q, ng_store):
        """自動で送れなかった会社(要手動・手で確かめる)に、会社ごとに置き換えた件名と全文を出す。送るのは本人の手"""
        p = safe_out(q.get("file", ""))
        if not p or not SENDER.exists():
            return self.send(400, {"error": "リストと送信元を先に決めてください"})
        import autosend
        sender = json.loads(SENDER.read_text(encoding="utf-8-sig"))
        manual = {url for (_n, url), r in sales_records.final_states(LOG).items() if r.get("結果") == "要手動"}
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
        todo, bodies, rejected = autosend.prepare_message_plan(rows[:50], MSG, sender)
        out = [{"company": t[0], "form": t[1], "subject": message_adoption.effective_sender(t[0], t[1], sender).get("件名", "").replace("{企業名}", t[0]), "message": b}
               for t, b in zip(todo, bodies)]
        return self.send(200, {"companies": out, "skipped": [{"company": n, "why": w} for n, _u, w in rejected],
                               "legal_missing": send_rules.legal_missing(sender), "window": send_rules.window_refusal()})

    def targets(self, q, ng_store=None):
        """送信対象の一覧と各社の文面(表示用)。送信の可否はここでは決めない(本番は確認ジョブの印が要る)"""
        import autosend
        p = safe_out(q.get("file", ""))
        if not p or not SENDER.exists():
            return self.send(400, {"error": "リストと送信元を先に決めてください"})
        try:
            mx = count(q.get("max"), 20, 1, 300)
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
            return self.send(200, {"lines": JOB["lines"][-300:], "running": JOB["running"], "name": JOB["name"], "code": JOB.get("code"), "files": outputs(), "version": VERSION, "here": str(HERE), "started": JOB.get("started", "")})
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
        if u.path == "/api/report":
            days = int(q["days"]) if str(q.get("days", "")).isdigit() and 1 <= int(q["days"]) <= 365 else 7
            return self.send(200, sales_records.report(LOG, days))
        if u.path == "/api/unsure":
            return self.send(200, sales_records.unsure(LOG))
        if u.path == "/api/manual_kit":
            return self.manual_kit(q, ng_store)
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
            mx = count(d.get("max"), 20, 1, 300)
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
            with LOCK:
                key_now, names = PREVIEW["key"], list(PREVIEW.get("names") or [])
            if key_now is None:
                return self.send(400, {"error": "先に「対象の会社と、送る文面の全文を確認する」を済ませてください"})
            if str(d.get("key", "")) != key_now[-1]:  # 画面が見せた確認番号と、いま有効な確認が同じときだけ(別の確認の承認にならない)
                return self.send(409, {"error": "画面の確認番号が古いです。「確認する」を押し直して、全文を読んでください"})
            # 承認は画面のボタンではなく、OSの確認ダイアログ(HTTPでは押せない)で人に聞く
            try:
                sender = json.loads(SENDER.read_text(encoding="utf-8-sig")) if SENDER.exists() else {}
            except ValueError:
                sender = {}
            shown = "\n".join("・" + n for n in names[:10]) + (f"\n ほか {len(names) - 10} 社" if len(names) > 10 else "")
            body = ("次の %d 社の問い合わせフォームに、画面に表示した文面を送ります。\n%s\n\n送信元: %s %s\n確認番号: %s\n\n"
                    "画面で全文を読んでいない場合は「いいえ」を押してください。\n「はい」で承認します。承認は1回の送信で使い切ります。"
                    % (len(names), shown, sender.get("会社名", ""), sender.get("氏名", ""), key_now[-1]))
            if not approval.ask_human("AIまるなげ営業 本番送信の承認", body):
                APPROVED["key"] = None
                return self.send(200, {"ok": False, "declined": True})
            if PREVIEW["key"] != key_now:  # ダイアログの間に確認が変わった
                APPROVED["key"] = None
                return self.send(409, {"error": "確認の内容が変わりました。もう一度「確認する」からやり直してください"})
            with LOCK:
                if PREVIEW["key"] != key_now:
                    APPROVED["key"] = None
                    return self.send(409, {"error": "確認の内容が変わりました。もう一度「確認する」からやり直してください"})
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
            r = sales_records.add_reply(name, url, str(d.get("kind", "")), str(d.get("text", "")), str(d.get("memo", ""))[:200])
            if "error" in r:
                return self.send(400, r)
            if d.get("kind") == "不要":  # 送らないでと言われた会社は、記録と同じ操作で送らないリストに入れる(片方だけが抜けないように)
                where = str(d.get("hp", "")).strip() or url
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
            if real and ng_store is None:
                return self.send(409, {"error": "送らない会社の記録(データベース)につながっていないため、本番の送信はできません。ツールを起動し直してください"})
            if real and NG_MIGRATION["error"]:
                return self.send(409, {"error": "送らない会社のリストを読み込めなかったため、本番の送信はできません。" + NG_MIGRATION["error"]})
            if real and _NG_SYNC and ng_store is not None and ng_store.migration_status() != "canonical":
                return self.send(409, {"error": "送らない会社の記録の引っ越しが済んでいないため、本番の送信はできません。" + (NG_MIGRATION["error"] or "ツールを起動し直してください")})
            try:
                key = (str(p), p.stat().st_mtime_ns, mx, bool(d.get("supp")), fingerprint(), digest(p, bool(d.get("supp")), mx, ng_store))
            except Exception as e:
                return self.send(400, {"error": "送る対象を数えられませんでした（送信元・リスト・文面を確かめてください）", "detail": repr(e)[:200]})
            if d.get("preview"):
                # 確認の出力に、印と同じ確認番号が出たときだけ印を立てる(見せた全文と印を一致させる)
                return self.send(200, {"started": run_job("確認", args + ["--preview"], on_ok=lambda k=key: set_preview(k) if f"確認番号: {k[-1]}" in JOB["lines"] else None)})
            if real:
                if d.get("confirm") != "送信":
                    return self.send(400, {"error": "本番は「送信」と入力してください"})
                if PREVIEW["key"] != key:
                    return self.send(400, {"error": "先に「対象の会社と、送る文面の全文を確認する」を押して、全文を読んでから送ってください（リスト・社数・文面・送信元・送らないリスト・送信済みの会社が変わって、送る会社や文面が確認と違うときも、もう一度確認が要ります）"})
                if APPROVED["key"] != key:
                    return self.send(400, {"error": "人の承認がありません。ツールの画面を開き、全文を読んで「この全文で送ることを承認する」を押してから、もう一度送ってください（AIは承認を押せません）"})
                PREVIEW.update(key=None, names=[])  # 確認は1回の本番ごとに使い切る
                APPROVED["key"] = None
                args += ["--send", "--expect", key[-1]]  # 送る本人が、確認時と同じ会社・全文・送信元かを照合し、違えば送らずに止まる
                token = approval.issue_ticket(key[-1])  # 承認のあとにだけ発行する使い切りの鍵。autosend --send はこれが無いと始まらない
                return self.send(200, {"started": run_job("本番送信", args, "送信\n", extra_env={approval.TICKET_ENV: token, approval.PORT_ENV: str(PORT), approval.UI_TOKEN_ENV: TOKEN})})
            return self.send(200, {"started": run_job("試走", args, None)})
        self.send(404, {"error": "なし"})


PAGE = (HERE / "ui.html").read_text(encoding="utf-8")  # 画面。デザインの変更はこのファイルだけで済む


if __name__ == "__main__":
    NO_BROWSER = bool(os.environ.get("FORMLIST_NO_BROWSER")) or "--no-browser" in sys.argv  # 画面を開かずに起動する(MCP・ログイン時の自動起動)
    OUT.mkdir(exist_ok=True)
    import socket
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            try:
                import urllib.request
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f"http://127.0.0.1:{PORT}/api/job", timeout=3) as r:
                    mine = json.loads(r.read().decode("utf-8")).get("here") in (None, str(HERE))
            except Exception:
                mine = False
            if not mine:
                print(f"{PORT}番は、このツールではない別のアプリが使っています。そのアプリを閉じるか、FORMLIST_PORT に8780〜8790の空いている番号を入れて起動してください")
                sys.exit(1)
            if not NO_BROWSER:  # MCP やログイン時の自動起動のときは画面を開かない
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
    if not NO_BROWSER:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


