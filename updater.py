"""公開リポジトリの最新版へ、画面から自動で更新する。実行環境: Windows / Mac / Linux, Python 3.9+, 標準ライブラリだけ。

流れ: manifest.json(ファイル名とsha256の一覧)を取る → 手元と違うファイルだけを、別の場所に取って検査する(sha256・Python の構文)
      → 1つでも検査に通らなければ、手元には何も書かず終わる → 通れば、前の版を「更新前の版」に残して1つずつ入れ替える
      → 画面のサーバーを起動し直す。起動し直した画面が応答しなければ、前の版に戻して起動し直す。
信じる範囲: 取得元は、導入と同じ公開リポジトリ(https)。導入の1行と同じ信頼で、署名は付けていない(リポジトリの持ち主を乗っ取られた場合は防げない)。
更新しないもの: 送信元・文面・送らないリスト・できたリスト・計測キー(manifest に載るのは、公開物の実行ファイルと説明書と、利用許可の公開鍵 license_sign_pub.pem の1つだけ。名前の検査もする)。
見ていないもの: 取得元が https の正規の応答かどうか(OSの証明書の検査に任せる)。MCP(mcp_server.py)は Claude が読み込んだまま動くので、更新しても Claude を開き直すまで古い。"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEV_MARK = HERE / "開発版_自動更新しない.txt"  # 開発用の作業フォルダの目印。公開物には含めない。あると、公開版で手元の変更を上書きしない(2026-10-29: 自動更新が未公開の変更を上書きした)
BASE = "https://raw.githubusercontent.com/joshicrea/joshicrea-formlist/master/"
UPDATABLE = {".py", ".html", ".md"}
PUBKEY_FILE = "license_sign_pub.pem"  # 利用許可の署名を確かめる公開鍵。この名前1つだけ、拡張子の許可の例外にする(.pem 全般は許さない)。鍵が差し替わると署名の検証が変わるので、公開は本番の署名と同梱鍵の一致を実導入で確かめてから
NO_UPDATE = {"sender.json", "message_adoptions.json", "sender.sample.json"}  # 本人の設定。万一 manifest に載っても触らない
KEEP_BACKUPS = 3
ADDED_NAME = "_新規.txt"
NAME_OK = re.compile(r"^[^/\\:*?\"<>|\x00-\x1f]+$")
WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
SHADOW_EXTRA = {"sitecustomize", "usercustomize", "site"}  # import されるだけで動く名前
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def base():
    """取得元。テストのときだけ、手元のフォルダ(file:)に差し替えられる。ほかの場所(http)へは差し替えられない"""
    b = os.environ.get("FORMLIST_UPDATE_BASE", "")
    return b if b.startswith("file:") else BASE


def _norm(b):
    return b.removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n")  # 改行の違い(CRLF/LF)だけで「古い」と出さない


def sha(b):
    return hashlib.sha256(_norm(b)).hexdigest()


DEV_ONLY = {"make_public_tree.py", "make_setting_text.py", "conftest.py"}  # 公開しない開発用のファイル


def runtime_files():
    """動いている版の印に入れるファイル。実行ファイル(.py)・画面(ui.html)・説明書。テストと開発用は入れない"""
    return sorted(p for p in HERE.iterdir() if p.is_file() and p.suffix in UPDATABLE and not p.name.startswith(("test_", ".")) and p.name not in DEV_ONLY)


def build():
    """この フォルダの版の印(12桁)。1つでも違うファイルがあれば変わる。画面のサーバーが「読み込んだ時点」の印と比べて、起動し直しが要るかを見る"""
    h = hashlib.sha256()
    for p in runtime_files():
        h.update(p.name.encode("utf-8") + b"\0" + _norm(p.read_bytes()) + b"\0")
    return h.hexdigest()[:12]


def _ssl_context():
    """python.org 版の Python(Mac)は OS の証明書を持たず、https が CERTIFICATE_VERIFY_FAILED になる。requests の依存で入る certifi があればそれを使う。無ければ既定のまま"""
    try:
        import ssl
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def fetch(name, timeout=8):
    url = base() + urllib.request.quote(name)
    ctx = _ssl_context() if url.startswith("https:") else None
    with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
        st = getattr(r, "status", None)  # file: の応答には番号が無い(None)。https は 200 でなければ失敗
        if st not in (None, 200):
            raise RuntimeError(f"応答 {st}")
        return r.read()


_STDLIB_FALLBACK = {"json", "os", "re", "sys", "time", "csv", "ssl", "socket", "http", "urllib", "email", "subprocess", "threading",
                    "pathlib", "shutil", "hashlib", "ctypes", "random", "string", "types", "typing", "logging", "sqlite3", "queue", "io"}  # Python 3.9 には stdlib_module_names が無い


def name_ok(name):
    """manifest の名前が、この フォルダ直下の実行ファイル・説明書・利用許可の公開鍵だけを指すか(上の階層・サブフォルダ・設定ファイルは拒否)"""
    if not (bool(NAME_OK.match(name)) and ".." not in name and (Path(name).suffix in UPDATABLE or name == PUBKEY_FILE) and name not in NO_UPDATE
            and not name.startswith(("test_", ".")) and len(name) <= 80):
        return False
    stem = Path(name).stem
    if stem.split(".")[0].lower() in WIN_RESERVED or "~" in name or stem != stem.rstrip(" .") or name != name.strip() or name.endswith("."):
        return False
    if name.endswith(".py") and (stem in SHADOW_EXTRA or stem in getattr(sys, "stdlib_module_names", ()) or stem in _STDLIB_FALLBACK):
        return False  # json.py など、標準ライブラリを隠す名前は入れない(import されるだけで別のコードが動く)
    return True


def _can_verify_signature():
    """公開鍵を読む部品(cryptography)がこの導入にあるか"""
    try:
        from cryptography.hazmat.primitives import serialization  # noqa: F401
        return True
    except Exception:
        return False


def _key_differs(want):
    try:
        return sha((HERE / PUBKEY_FILE).read_bytes()) != want
    except OSError:
        return True


def fetch_manifest():
    """公開の manifest.json。取れない・形が違うときは None(「最新」とは言わない)"""
    try:
        body = fetch("manifest.json")
        if not body.strip() or body.lstrip().startswith(b"<"):  # 空・HTML(プロキシやログインの画面)は本物の応答ではない
            return None
        m = json.loads(body.decode("utf-8-sig"))
        files = m.get("files")
        if not isinstance(files, dict) or not files:
            return None
        if any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for k, v in files.items()):
            return None
        keep = {k: v for k, v in files.items() if name_ok(k)}
        key_skipped = False
        if PUBKEY_FILE in keep and not _can_verify_signature():
            if _key_differs(keep[PUBKEY_FILE]):
                key_skipped = True  # 鍵が変わっているのに入れられない。画面に出す(無言にしない)
            keep.pop(PUBKEY_FILE)  # 暗号の部品が無い導入へ鍵を入れると、どの署名も合わず止まり続ける(更新では部品を入れられない)。鍵を入れず、今までの動きのまま
        return {"version": str(m.get("version", ""))[:40], "files": keep, "key_skipped": key_skipped}
    except Exception:
        return None


def changed_files(m):
    out = []
    for name, want in sorted(m["files"].items()):
        p = HERE / name
        try:
            if sha(p.read_bytes()) != want:
                out.append(name)
        except OSError:
            out.append(name)  # 手元に無い(新しい版で増えたファイル)
    return out


SKIP_FILE = "更新_戻した版.txt"
MAX_TRIES = 5  # 同じ版を、更新後に画面が立たず戻した回数の上限。超えたら、次の版が公開されるまで入れ直さず「失敗」を画面に出す
RETRY_COOLDOWN_SEC = 600  # 戻した直後に同じ版を入れ直さない時間(戻した版が立ち上がる前に再挑戦して、無限に戻し続けるのを防ぐ。定期の確認は30分ごとなので、次の確認で再挑戦する)


def skip_info():
    """戻した版の記録 {"version","count","last"}。無ければ None。古い形式(版の文字だけ)は、1回戻した・ファイルの更新時刻を最後の時刻として読む"""
    p = HERE / "出力" / SKIP_FILE
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        j = json.loads(raw)
        if isinstance(j, dict) and j.get("version"):
            return {"version": str(j["version"]), "count": int(j.get("count", 1)), "last": float(j.get("last", 0))}
    except (ValueError, TypeError):
        pass
    try:
        return {"version": raw.splitlines()[0], "count": 1, "last": p.stat().st_mtime}
    except OSError:
        return None


def skipped_version():
    i = skip_info()
    return i["version"] if i else ""


def skip_decision(remote_version):
    """公開されている版が、前に戻した版と同じときの扱い: "none"(同じ版ではない) / "retry"(再挑戦してよい) / "wait"(戻した直後・少し待つ) / "failed"(上限回数戻した)"""
    i = skip_info()
    if not i or not remote_version or i["version"] != remote_version:
        return "none"
    if i["count"] >= MAX_TRIES:
        return "failed"
    return "retry" if time.time() - i["last"] >= RETRY_COOLDOWN_SEC else "wait"


STATUS_FILE = "更新の状態.json"


def write_status(state, message):
    """直近の更新の確認・試みの結果を、出力/ に残す(画面が読む)。常駐の自動更新の失敗が、無言にならないように"""
    try:
        p = HERE / "出力" / STATUS_FILE
        p.parent.mkdir(exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"state": state, "message": message, "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def read_status():
    try:
        j = json.loads((HERE / "出力" / STATUS_FILE).read_text(encoding="utf-8"))
        return j if isinstance(j, dict) else None
    except (OSError, ValueError):
        return None


def check():
    if DEV_MARK.exists():
        return {"state": "開発版", "files": [], "message": "開発用の作業フォルダのため、自動では更新しません"}
    m = fetch_manifest()
    if m is None:
        return {"state": "未確認", "files": [], "message": "公開されている最新の版と照合できませんでした(通信できない・社内ネットワークで止められた等)。最新かどうかは分かりません。ツールはこのまま使えます"}
    ch = changed_files(m)
    out = {"files": ch, "remote_version": m["version"]}
    if m.get("key_skipped"):
        out["key_skipped"] = True
        out["key_message"] = "利用許可の公開鍵が新しくなっていますが、このパソコンには署名を確かめる部品(cryptography)が入っていないため、鍵を更新していません。利用許可の確認が通らないときは、担当者に連絡してください"
    dec = skip_decision(m["version"]) if ch else "none"
    if dec == "failed":
        i = skip_info()
        out.update(state="失敗", message=f"公開されている新しい版は、{i['count']}回とも更新のあと画面が立ち上がらず、前の版へ戻しました。前の版のまま使えます。次の版が公開されるまで、更新しません。担当者に連絡してください")
    elif dec == "wait":
        out.update(state="見送り", message="公開されている新しい版は、前に更新したあと画面が立ち上がらなかったため、前の版へ戻しました。しばらくしてから、もう一度更新します")
    elif ch:
        out.update(state="古い", message=f"新しい版があります（更新するファイル {len(ch)}個）")
    else:
        out.update(state="最新", message="公開されている最新の版と同じです")
    return out


def _stage(m, names, stage):
    """変えるファイルを別の場所に取り、検査する。検査に落ちたら理由つきで ValueError(手元には何も書いていない)"""
    stage.mkdir(parents=True, exist_ok=True)
    got = {}
    for n in names:
        try:
            b = fetch(n, timeout=20)
        except Exception as e:
            raise ValueError(f"{n} を取得できませんでした（{type(e).__name__}: {e}）")
        if sha(b) != m["files"][n]:
            raise ValueError(f"{n} の中身が manifest と合いません。取得の途中で変わったか、通信の途中で書き換えられた可能性があります。何も入れ替えていません")
        if n.endswith(".py"):
            try:
                compile(b.removeprefix(b"\xef\xbb\xbf").decode("utf-8"), n, "exec")
            except (SyntaxError, UnicodeDecodeError, ValueError) as e:
                raise ValueError(f"{n} が Python として読めません（{e}）。何も入れ替えていません")
        if n == PUBKEY_FILE and (b"-----BEGIN PUBLIC KEY-----" not in b or b"PRIVATE" in b):
            raise ValueError(f"{n} が公開鍵の形をしていません。何も入れ替えていません")
        (stage / n).write_bytes(b)
        got[n] = b
    return got


LOCK_STALE_SEC = 600


def _lock_path():
    return HERE / "出力" / ".更新中.lock"


def _acquire():
    """別のプロセス(別の画面・別の更新)が更新中なら False。10分より古いロックは、落ちた更新の残りとして取り直す"""
    lp = _lock_path()
    lp.parent.mkdir(exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - lp.stat().st_mtime > LOCK_STALE_SEC:
                    lp.unlink()
                    continue
            except OSError:
                pass
            return False
        except OSError:
            return False
    return False


def _release():
    try:
        _lock_path().unlink()
    except OSError:
        pass


def apply(log=None):
    """最新版へ入れ替える。戻りは {"updated": [...], "backup": フォルダ}。入れ替えなかったときは ValueError か、updated が空"""
    log = log or (lambda s: None)
    if not _acquire():
        raise ValueError("ほかの画面が更新している最中です。少し待ってから、もう一度開いてください")
    try:
        return _apply(log)
    finally:
        _release()


def _apply(log):
    if DEV_MARK.exists():
        raise ValueError("開発用の作業フォルダのため、更新しません(手元の変更が公開版で上書きされるのを防ぐ目印があります)")
    m = fetch_manifest()
    if m is None:
        raise ValueError("公開されている最新の版の一覧(manifest.json)を取得できませんでした。通信を確かめてください")
    names = changed_files(m)
    if not names or skip_decision(m["version"]) in ("wait", "failed"):  # 再挑戦してよい(retry)ときは入れ替える
        return {"updated": [], "backup": "", "version": m["version"]}
    ts = time.strftime("%Y%m%d_%H%M%S")
    stage = HERE / "出力" / ".更新中" / ts
    try:
        got = _stage(m, names, stage)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)  # 検査に通らなかった分は残さない(手元のファイルには、まだ何も書いていない)
        raise
    backup = HERE / "更新前の版" / ts
    backup.mkdir(parents=True, exist_ok=True)
    added = []
    for n in names:
        if (HERE / n).exists():
            shutil.copy2(HERE / n, backup / n)
        else:
            added.append(n)
    (backup / ADDED_NAME).write_text("\n".join(added), encoding="utf-8")  # 新しい版で増えたファイル。戻すときは消す
    done = []
    try:
        for n in names:
            tmp = HERE / (n + ".更新中")
            tmp.write_bytes(got[n])
            os.replace(tmp, HERE / n)
            done.append(n)
    except Exception:
        restore(backup, done)  # 途中で失敗したら、入れ替えた分を戻す
        raise
    shutil.rmtree(stage, ignore_errors=True)
    olds = sorted(p for p in (HERE / "更新前の版").iterdir() if p.is_dir())
    for p in olds[:-KEEP_BACKUPS]:
        shutil.rmtree(p, ignore_errors=True)
    rec = HERE / "出力" / "更新の記録.txt"
    rec.parent.mkdir(exist_ok=True)
    with open(rec, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 更新 {len(done)}個: {', '.join(done)}（前の版: 更新前の版/{ts}）\n")
    log(f"更新しました（{len(done)}個）")
    return {"updated": done, "backup": str(backup), "version": m["version"]}


def restore(backup, names=None):
    """前の版に戻す。names が無ければ、バックアップにあるファイルを全部戻す"""
    backup = Path(backup)
    for p in backup.iterdir():
        if p.name == ADDED_NAME or (names is not None and p.name not in names):
            continue
        tmp = HERE / (p.name + ".更新中")
        shutil.copy2(p, tmp)
        os.replace(tmp, HERE / p.name)
    try:
        for n in (backup / ADDED_NAME).read_text(encoding="utf-8").split():
            if names is None or n in names:
                (HERE / n).unlink(missing_ok=True)
    except OSError:
        pass


# 起動し直しを受け持つ小さなプログラム。画面のサーバーが終わったあとに、新しい版で起動し直す。
# 新しい版が wait 秒たっても応答しない、または途中で終了したときは、前の版に戻して、もう一度起動し直す(更新で画面が立たなくなるのを防ぐ)。
# 戻した版は、回数と時刻を出力/更新_戻した版.txt に残す(次の確認で再挑戦する。上限回数で「失敗」として画面に出す)。立ち上がったら、その記録は消す
_RELAUNCH = r"""
import os, sys, time, json, shutil, subprocess, urllib.request
here, port, backup, exe = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
version = sys.argv[5] if len(sys.argv) > 5 else ""
wait = float(sys.argv[6]) if len(sys.argv) > 6 else 120.0
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
skip = os.path.join(here, "出力", "更新_戻した版.txt")
def up():
    try:
        with opener.open("http://127.0.0.1:%s/api/whoami" % port, timeout=2) as r:
            return json.loads(r.read().decode("utf-8")).get("tool") == "formlist"
    except Exception:
        return False
def start():
    env = dict(os.environ, FORMLIST_PORT=port, FORMLIST_NO_BROWSER="1", PYTHONIOENCODING="utf-8")
    log = open(os.path.join(here, "出力", "app_起動ログ.txt"), "ab")
    return subprocess.Popen([exe, "-X", "utf8", os.path.join(here, "app.py")], cwd=here, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=(os.name != "nt"))
for _ in range(60):
    if not up():
        break
    time.sleep(0.5)
proc = start()
deadline = time.time() + wait
while time.time() < deadline:
    time.sleep(0.5)
    if up():
        if version:
            try:
                j = json.load(open(skip, encoding="utf-8"))
                if isinstance(j, dict) and j.get("version") == version:
                    os.remove(skip)
            except Exception:
                pass
        sys.exit(0)
    if proc.poll() is not None:
        break
if not (backup and os.path.isdir(backup)):
    sys.exit(1)
try:
    proc.kill()
    proc.wait(timeout=10)
except Exception:
    pass
for n in os.listdir(backup):
    if n.startswith("_"):
        continue
    shutil.copy2(os.path.join(backup, n), os.path.join(here, n + ".更新中"))
    os.replace(os.path.join(here, n + ".更新中"), os.path.join(here, n))
try:
    for n in open(os.path.join(backup, "_新規.txt"), encoding="utf-8").read().split():
        p = os.path.join(here, n)
        if os.path.isfile(p):
            os.remove(p)
except OSError:
    pass
count = 1
try:
    j = json.load(open(skip, encoding="utf-8"))
    if isinstance(j, dict) and j.get("version") == version:
        count = int(j.get("count", 0)) + 1
except Exception:
    pass
with open(os.path.join(here, "出力", "更新の記録.txt"), "a", encoding="utf-8") as f:
    f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " 更新後に画面が立たなかったため、前の版に戻しました(%s秒待った・この版で%s回目)\n" % (int(wait), count))
if version:
    with open(skip, "w", encoding="utf-8") as f:
        json.dump({"version": version, "count": count, "last": time.time()}, f)
start()
"""


RELAUNCH_WAIT_S = 180  # 新しい版が応答するまで待つ秒数。実測して決めた(報告参照)。起動の途中で終了したときは、待たずに戻す


def relaunch_wait():
    try:
        return float(os.environ.get("FORMLIST_RELAUNCH_WAIT") or RELAUNCH_WAIT_S)
    except ValueError:
        return float(RELAUNCH_WAIT_S)


def relaunch(port, backup, exe=None, version=""):
    """このプロセスが終わったあとに、新しい版で画面のサーバーを起動し直す小さなプログラムを、別に起動する"""
    exe = exe or sys.executable
    if exe.lower().endswith("python.exe"):
        w = exe[:-len("python.exe")] + "pythonw.exe"
        exe = w if os.path.exists(w) else exe
    subprocess.Popen([exe, "-X", "utf8", "-c", _RELAUNCH, str(HERE), str(port), str(backup), exe, str(version), str(relaunch_wait())],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
                     start_new_session=(os.name != "nt"))  # Mac/Linux: 親(画面のサーバー)が終わっても、この小さなプログラムは生きて起動し直す
