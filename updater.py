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
            try:  # 例外の原文は画面に出さず、更新の記録に残す(波2 D)
                rec = HERE / "出力" / "更新の記録.txt"
                rec.parent.mkdir(exist_ok=True)
                with open(rec, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {n} の取得に失敗(原文): {type(e).__name__}: {e}\n")
            except OSError:
                pass
            raise ValueError(f"{n} を取得できませんでした。インターネットにつながっているかを確かめて、少し待ってからもう一度更新します（くわしい理由は 出力/更新の記録.txt に残しました）")
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
    try:
        return _replace_with_backup(m, names, got, stage, backup, ts, log)
    except Exception as e:
        try:
            e.formlist_backup = str(backup)  # 画面が、手元のファイルが前の版のままかを、この控えの一覧で確かめる(Codex 2周目 弱点)
        except Exception:
            pass
        raise


class BackupError(ValueError):
    """控えを作れなかった(手元のファイルはまだ1つも入れ替えていない)"""


def _replace_with_backup(m, names, got, stage, backup, ts, log):
    backup.mkdir(parents=True, exist_ok=True)
    # 控えの一覧は、入れ替える前の元のファイルから作る(控えに残ったファイルから作ると、欠けた控えを正常として固定する。Codex 2周目 致命傷1)
    originals, added = {}, []
    for n in names:
        if (HERE / n).exists():
            originals[n] = _restore_sha(HERE / n)
        else:
            added.append(n)
    for n in originals:
        shutil.copy2(HERE / n, backup / n)
    (backup / ADDED_NAME).write_text("\n".join(added), encoding="utf-8")  # 新しい版で増えたファイル。戻すときは消す
    write_backup_list(backup, originals, added)
    for n, want in originals.items():  # 控えの写しが一覧と合わなければ、何も入れ替えない(戻せない更新をしない)
        try:
            ok = _restore_sha(backup / n) == want
        except OSError:
            ok = False
        if not ok:
            raise BackupError("前の版の控えを正しく作れませんでした。何も入れ替えていません")
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
        if p.name.startswith("_") or (names is not None and p.name not in names):  # _新規.txt・_一覧.json は控えの管理用で、戻すファイルではない
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


BACKUP_LIST = "_一覧.json"

# 前の版へ戻す処理(1つの実装)。起動し直しのプログラム(_RELAUNCH)にも、そのまま埋め込む(別のプロセスで、新しい版のファイルを読まずに動くため)。
# 1) 控えの一覧(_一覧.json)が無い・読めない・空なら、何も戻さず「戻せなかった」とする(空の控えを「全部戻せた」と扱わない。Codex 致命傷B)
# 2) 一覧の各ファイルは、控えの中身が一覧の sha256 と一致することと、戻した後の中身が一致することを確かめる
# 3) 起動の止め(startup_guard)を持つ updater.py・app.py は最後に、この順で戻す。ほかが1つでも戻せなければ、この2つは新しい版のまま残し、
#    次の起動で止まれるようにする(旧 app.py には止めが無いので、先に戻すと混ざった版が動く。Codex 致命傷A)
_RESTORE_SRC = r"""
def _restore_sha(p):
    import hashlib
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()
def restore_backup(here, backup):
    import os, json, shutil
    guard = ("updater.py", "app.py")
    failed, raw, restored = [], [], []
    try:
        with open(os.path.join(backup, "_一覧.json"), encoding="utf-8") as f:
            man = json.load(f)
        files, added = man["files"], man["added"]
        if not isinstance(files, dict) or not isinstance(added, list) or not (files or added):
            raise ValueError("backup list is empty")
    except Exception as e:
        return ["前の版の写しの一覧(_一覧.json)"], ["backup list: %r" % (e,)], []
    def put(n):
        dst = os.path.join(here, n)
        tmp = dst + ".更新中"
        keep = None  # 起動の止めを持つファイル(updater.py・app.py)の、置き換える前の中身(新しい版)
        try:
            if os.sep in n or "/" in n or n.startswith(("_", ".")):
                raise ValueError("bad name")
            src = os.path.join(backup, n)
            if _restore_sha(src) != files[n]:
                raise ValueError("backup copy differs from the list")
            if n in guard and os.path.isfile(dst):
                with open(dst, "rb") as f:
                    keep = f.read()
            shutil.copy2(src, tmp)
            if _restore_sha(tmp) != files[n]:  # 置き換える前に照合する(置き換えた後に照合を待つ隙間を作らない。Codex 2周目 致命傷2)
                raise ValueError("temporary copy differs from the list")
            os.replace(tmp, dst)
            try:
                same = _restore_sha(dst) == files[n]
            except OSError:  # 置き換えた直後に読めない(一時的な掴み・読み取り障害)。照合できないので食い違いと同じ扱いにする(Codex 3周目 致命傷)
                same = False
            if not same:
                if keep is not None:  # 置き換えた後に外から書き換えられた・照合できない。起動の止めを持つ新しい版を書き戻し、次の起動で止まれるようにする
                    with open(tmp, "wb") as f:
                        f.write(keep)
                    os.replace(tmp, dst)
                raise ValueError("restored file differs from the list")
            restored.append(n)
        except Exception as e:
            failed.append(n)
            raw.append("%s: %r" % (n, e))
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)  # 写しかけを本体のフォルダに残さない
            except OSError:
                pass
    for n in sorted(files):
        if n not in guard:
            put(n)
    for n in added:
        p = os.path.join(here, str(n))
        try:
            if os.path.isfile(p):
                os.remove(p)
            if os.path.exists(p):
                raise OSError("still exists")
        except Exception as e:
            failed.append("%s(新しい版で増えたファイル・消せなかった)" % n)
            raw.append("%s: %r" % (n, e))
    for n in guard:
        if n in files:
            if failed:
                failed.append("%s(ほかを戻せなかったため、次の起動を止めるよう新しい版のまま)" % n)
            else:
                put(n)
    return failed, raw, restored
"""
exec(_RESTORE_SRC)  # restore_backup をこのモジュールでも使う(次の起動の戻し直し・試験)


def write_backup_list(backup, files, added=()):
    """控えの一覧を書く。files = {名前: 入れ替える前の元のファイルの sha256}(控えに残ったファイルからは作らない。Codex 2周目 致命傷1)、
    added = 新しい版で増えたファイルの名前"""
    backup = Path(backup)
    files = {str(k): str(v) for k, v in dict(files).items()}
    tmp = backup / (BACKUP_LIST + ".tmp")
    tmp.write_text(json.dumps({"files": files, "added": [str(a) for a in added]}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, backup / BACKUP_LIST)


def matches_backup_list(backup, here=None):
    """手元のファイルが控えの一覧のとおり(入れ替える前の版)か。一覧の各ファイルの sha256 が一致し、新しい版で増えたファイルが無いとき True。
    一覧が無い・読めない・空なら False(確かめられないときに「前の版のまま」と言わない)"""
    here = Path(here or HERE)
    try:
        lst = json.loads((Path(backup) / BACKUP_LIST).read_text(encoding="utf-8"))
        files, added = lst["files"], lst["added"]
        if not isinstance(files, dict) or not isinstance(added, list) or not (files or added):
            return False
        return all(_restore_sha(here / n) == v for n, v in files.items()) and not any((here / str(a)).exists() for a in added)
    except Exception:
        return False


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
#@RESTORE@
# 戻せなかったファイル。1つ失敗しても残りは戻す。1つでも残れば、新旧の混ざった版は起動しない(波2 F)。
# 控えの一覧で照合し、updater.py・app.py は最後に戻す(ほかが戻せなければ新しい版のまま残し、次の起動で止める。Codex 致命傷A・B)
failed, raw, _restored = restore_backup(here, backup)
count = 1
try:
    j = json.load(open(skip, encoding="utf-8"))
    if isinstance(j, dict) and j.get("version") == version:
        count = int(j.get("count", 0)) + 1
except Exception:
    pass
now = time.strftime("%Y-%m-%d %H:%M:%S")
sid = "U" + time.strftime("%m%d-%H%M%S")  # 受付番号(画面のサーバーが立たないので、ここで決めて記録とお知らせに書く)
try:
    with open(os.path.join(here, "出力", "更新の記録.txt"), "a", encoding="utf-8") as f:
        if failed:
            f.write(now + " 更新後に画面が立たなかったため前の版に戻そうとしましたが、戻せなかったファイルがあります(ほかのプログラムが開いていた等): %s。前の版の写しは %s に残っています。新旧が混ざるので起動していません(受付番号 %s)\n" % ("、".join(failed), backup, sid))
        else:
            f.write(now + " 更新後に画面が立たなかったため、前の版に戻しました(%s秒待った・この版で%s回目)\n" % (int(wait), count))
except OSError:
    pass
try:
    if version:
        with open(skip, "w", encoding="utf-8") as f:
            json.dump({"version": version, "count": count, "last": time.time()}, f)
except OSError:
    pass
if not failed:
    start()
    sys.exit(0)
# 戻しきれなかった。新旧の混ざった版で送信や送信状態の記録を動かさないよう、起動せずに止まり、利用者に日本語で知らせる(波2 F)
notice = os.path.join(here, "出力", "前の版に戻せませんでした.txt")
try:  # 次の起動の入口(app.py の最初・startup_guard)がこの印を見て、戻しをやり直す。戻せなければ起動しない(破壊検証3 致命傷1)
    with open(os.path.join(here, "出力", "前の版に戻せませんでした.json"), "w", encoding="utf-8") as f:
        json.dump({"backup": backup, "sid": sid, "failed": failed, "at": now}, f, ensure_ascii=False)
except OSError:
    pass
try:
    with open(os.path.join(here, "出力", "サポート記録.txt"), "a", encoding="utf-8") as f:
        f.write("===== 受付番号 %s %s updater_rollback\n戻せなかったファイル: %s\n前の版の写し: %s\n%s\n" % (sid, now, "、".join(failed), backup, "\n".join(raw)))
except OSError:
    pass
try:
    with open(notice, "w", encoding="utf-8") as f:
        f.write("フォームリスト作成ツールの更新に失敗し、前の版にも全部は戻せませんでした(%s)。\n"
                "新しい版と前の版が混ざった状態で動かすと、送信の記録が正しく残らないおそれがあるため、ツールは起動していません。\n\n"
                "次にすること:\n"
                "1. パソコンを再起動してから、ツールをもう一度起動してください(ほかのプログラムがファイルを開いていたことが多い原因です)。"
                "起動するときに、前の版へ戻すのをもう一度試します。戻しきれないときは、混ざった版では起動せず、この案内をもう一度出します。\n"
                "2. それでも起動しないときは、この文と受付番号を担当者に送ってください。\n\n"
                "受付番号: %s\n戻せなかったファイル: %s\n前の版の写しの場所: %s\n" % (now, sid, "、".join(failed), backup))
except OSError:
    pass
if not os.environ.get("FORMLIST_NO_NOTICE_OPEN"):  # 試験では開かない
    try:
        if os.name == "nt":
            os.startfile(notice)
        else:
            subprocess.Popen(["open", notice], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
sys.exit(1)
"""


_RELAUNCH = _RELAUNCH.replace("#@RESTORE@\n", _RESTORE_SRC)
assert "def restore_backup" in _RELAUNCH


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


# ---- 戻し失敗の印と、次の起動での戻し直し(破壊検証3 致命傷1) ----
# 起動し直しのプログラム(_RELAUNCH)が前の版へ戻しきれなかったとき、出力/前の版に戻せませんでした.json(と .txt のお知らせ)を残す。
# 次にツールが動き出すとき(app.py の最初・ほかの部品を読み込む前)に startup_guard がこの印を見て、前の版の写しから戻しをやり直す。
# 全部戻せたら印を消して起動し直す。戻せなければ、新旧の混ざった版では起動せず、決まった日本語と受付番号を出して止まる(原文はサポート記録へ)。
FAIL_MARK = "前の版に戻せませんでした.json"
FAIL_NOTICE = "前の版に戻せませんでした.txt"


def _fail_backup(out):
    """印から、前の版の写しの場所を読む。.json が無い(古い版が残した .txt だけ)ときは、お知らせの「前の版の写しの場所:」の行から読む"""
    try:
        j = json.loads((out / FAIL_MARK).read_text(encoding="utf-8"))
        if isinstance(j, dict) and j.get("backup"):
            return str(j["backup"])
    except (OSError, ValueError):
        pass
    try:
        for line in (out / FAIL_NOTICE).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("前の版の写しの場所:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def recover_failed_rollback(here=None):
    """戻し失敗の印があれば、前の版の写しから戻しをやり直す。
    戻り: None=印が無い(何もしない) / {"ok": True, "restored": [...]} / {"ok": False, "sid": 受付番号, "failed": [...], "raw": [原文]}"""
    here = Path(here or HERE)
    out = here / "出力"
    if not ((out / FAIL_MARK).exists() or (out / FAIL_NOTICE).exists()):
        return None
    backup = _fail_backup(out)
    failed, raw, restored = [], [], []
    if not backup or not os.path.isdir(backup):
        failed.append("前の版の写し(%s)" % (backup or "場所が分からない"))
        raw.append("backup not found: %r" % backup)
    else:
        failed, raw, restored = restore_backup(str(here), backup)  # 一覧で照合・updater.py と app.py は最後(Codex 致命傷A・B)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if not failed:
        for f in (FAIL_MARK, FAIL_NOTICE):
            try:
                (out / f).unlink()
            except OSError:
                pass
        try:
            with open(out / "更新の記録.txt", "a", encoding="utf-8") as f:
                f.write(f"{now} 起動のときに、前の版へ戻すのをやり直し、全部戻しました({len(restored)}個)。前の版で起動します\n")
        except OSError:
            pass
        return {"ok": True, "restored": restored}
    sid = "U" + time.strftime("%m%d-%H%M%S")
    try:
        out.mkdir(exist_ok=True)
        with open(out / "サポート記録.txt", "a", encoding="utf-8") as f:
            f.write("===== 受付番号 %s %s startup_rollback_retry\n前の版の写し: %s\n%s\n" % (sid, now, backup, "\n".join(raw)))
    except OSError:
        pass
    try:
        with open(out / "更新の記録.txt", "a", encoding="utf-8") as f:
            f.write(f"{now} 起動のときに前の版へ戻すのをやり直しましたが、戻せなかったファイルがあります: {'、'.join(failed)}。新旧が混ざるので起動していません(受付番号 {sid})\n")
    except OSError:
        pass
    try:
        (out / FAIL_NOTICE).write_text(
            "フォームリスト作成ツールの更新に失敗し、前の版にも全部は戻せていません(%s に起動したときの確認)。\n"
            "新しい版と前の版が混ざった状態で動かすと、送信の記録が正しく残らないおそれがあるため、ツールは起動していません。\n\n"
            "次にすること:\n"
            "1. パソコンを再起動してから、ツールをもう一度起動してください(ほかのプログラムがファイルを開いていたことが多い原因です)。"
            "起動するときに、前の版へ戻すのをもう一度試します。\n"
            "2. それでも起動しないときは、この文と受付番号を担当者に送ってください。\n\n"
            "受付番号: %s\n戻せなかったファイル: %s\n前の版の写しの場所: %s\n" % (now, sid, "、".join(failed), backup or "不明"), encoding="utf-8")
    except OSError:
        pass
    return {"ok": False, "sid": sid, "failed": failed, "raw": raw}


def startup_message(sid):
    """戻せなかったときに、起動の入口が出す決まった日本語(例外の原文は入れない)"""
    return ("フォームリスト作成ツールは、更新の失敗で新しい版と前の版が混ざったままのため、起動していません。"
            "前の版へ戻すのをもう一度試しましたが、戻せないファイルがありました。"
            "パソコンを再起動してから、もう一度起動してください。それでも起動しないときは、"
            f"出力フォルダの「{FAIL_NOTICE}」と受付番号 {sid} を担当者に送ってください。")


def startup_guard(argv=None, exit_code=1, here=None):
    """ツールが動き出す入口(app.py の最初)で呼ぶ。印が無ければ何もしない。
    戻しをやり直して全部戻せたら、戻した版で同じ入口を起動し直して、この(読み込み済みの)プロセスは終わる。
    戻せなければ、決まった日本語と受付番号を出して exit_code で終わる(混ざった版では起動しない)"""
    here = Path(here or HERE)
    r = recover_failed_rollback(here)
    if r is None:
        return None
    argv = list(sys.argv if argv is None else argv)
    if r["ok"]:
        if os.environ.get("FORMLIST_ROLLBACK_RESTARTED"):  # 起動し直した先でまた印があった(起こらないはず)。続けて起動し直さない
            return r
        env = dict(os.environ, FORMLIST_ROLLBACK_RESTARTED="1")
        script = os.path.abspath(argv[0]) if argv else str(here / "app.py")
        subprocess.Popen([sys.executable, "-X", "utf8", script] + argv[1:], cwd=str(here), env=env, creationflags=NO_WINDOW,
                         start_new_session=(os.name != "nt"))
        sys.exit(0)
    msg = startup_message(r["sid"])
    try:
        with open(here / "出力" / "app_起動ログ.txt", "ab") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 起動を止めました: {msg}\n".encode("utf-8"))
    except OSError:
        pass
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass
    show = "--no-browser" not in argv and not os.environ.get("FORMLIST_NO_BROWSER") and not os.environ.get("FORMLIST_NO_NOTICE_OPEN")
    if show:  # 画面から起動したときだけ、お知らせを開く(見張り・MCP からの起動では開かない。記録には残る)
        try:
            if os.name == "nt":
                os.startfile(str(here / "出力" / FAIL_NOTICE))
            else:
                subprocess.Popen(["open", str(here / "出力" / FAIL_NOTICE)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    sys.exit(exit_code)
