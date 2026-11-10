"""画面のサーバー(app.py)を置く番号と、その番号の使われ方の判定。画面(app.py)・MCP(mcp_server.py)・導入(install.ps1)が同じ定義を使う。実行環境: Windows / Python 3.9+。
1つのサーバーは同時に1つの「探す」しか動かさない。別の条件で同時に探すときは、別の番号でもう1つサーバーを立てる(最大3つ)。
3つまでにしているのは、1つのサーバーが最大3本のブラウザを動かすため、増やすとパソコンが重くなり、Googleにも止められやすくなるから。
画面の入口は 8780 に固定する(ブックマークしても変わらない)。8781・8782 は、同時に探すときの追加の画面。"""
import json
import socket
import urllib.request

POOL = [8780, 8781, 8782]
MAIN = POOL[0]
PER_SERVER_MAX = 3  # 1つのサーバーが同時に動かす地図の取得の本数の上限。同時に取る総量は 台数(POOL) × この値 = 9本まで。どちらも定数で、画面や設定からは変えられない
_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 127.0.0.1 宛ての通信を、会社のプロキシに回さない


def can_bind(port):
    """その番号が本当に空いているか。他のアプリが使っている・Windowsが予約している(Hyper-V/WSL/Docker)ときは False"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def job(port, timeout=3):
    """その番号で動いているこのツールの /api/whoami の中身(here・version・port・running・name)。このツールでなければ None。
    /api/job はトークンが要るので、トークン無しの確認には使えない(使うと、動いている自分のツールを「別のアプリ」と取り違える)"""
    try:
        with _OPEN.open(f"http://127.0.0.1:{port}/api/whoami", timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
        return j if isinstance(j, dict) and j.get("tool") == "formlist" else None
    except Exception:
        return _legacy(port, timeout)  # /api/whoami が無い古い版(すでに配ったもの)かもしれない


def _legacy(port, timeout=3):
    """/api/whoami を持たない古い版の画面か。トップページに埋め込まれたトークンで /api/job を読み、同じ形(here・version・running・name)で返す。
    古い版を「別のアプリ」と取り違えると、新しい版が起動を断り、古い版が居座り続ける"""
    import re
    try:
        with _OPEN.open(f"http://127.0.0.1:{port}/", timeout=timeout) as r:
            html = r.read(400000).decode("utf-8", "replace")
        m = re.search(r"const TOKEN='([^']+)'", html)
        if not m or "AIまるなげ営業" not in html:
            return None
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/job", headers={"X-Token": m.group(1)})
        with _OPEN.open(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
        if not isinstance(j, dict) or "lines" not in j:
            return None
        return {"tool": "formlist", "here": j.get("here"), "version": j.get("version"), "port": port, "running": bool(j.get("running")), "name": j.get("name", ""), "legacy": True}
    except Exception:
        return None


DEV_MARK = "開発版_自動更新しない.txt"
REQUIRED = ("app.py", "ui.html", "ports.py", "updater.py", "settings_dir.py")  # 導入先に無ければ、壊れているとみなして振り向けない(R6)
MANIFEST = ".install_files.txt"  # install.ps1 が書く、導入したファイルの一覧
VERSION_FILES = ("app.py", "autosend.py", "ng_list.py", "site_checker.py")


def disk_version(here):
    """そのフォルダのファイルから計算した版(画面のサーバーが起動時に whoami の version として返すものと同じ式)。
    動いている画面の version とディスクの版が違えば、ファイルは新しいのに古い版が動いている。読めなければ ""(比べない)"""
    import hashlib
    from pathlib import Path
    try:
        return hashlib.sha256(b"".join((Path(here) / f).read_bytes() for f in VERSION_FILES)).hexdigest()[:12]
    except OSError:
        return ""


def installed_dir(environ=None):
    """導入先のコピーのフォルダ(install.ps1 が置く場所)。環境変数 FORMLIST_INSTALL_DIR があればそちら。
    起動の入口(app.py・install.ps1 -Open・ブラウザで起動.bat 経由の app.py)は、この1つの定義を使う(R26)"""
    import os
    from pathlib import Path
    env = os.environ if environ is None else environ
    return Path(env.get("FORMLIST_INSTALL_DIR") or (Path.home() / ".claude" / "formlist" / "app"))


def install_broken(inst):
    """導入先が壊れていれば理由の文、無事なら ""(R6)。必須ファイルと、導入したファイルの一覧(.install_files.txt)の実在を見る"""
    from pathlib import Path
    inst = Path(inst)
    lost = [n for n in REQUIRED if not (inst / n).is_file()]
    try:
        names = [x.strip().lstrip("﻿") for x in (inst / MANIFEST).read_text(encoding="utf-8-sig").splitlines() if x.strip()]
    except OSError:
        names = []
    lost += [n for n in names if n not in lost and not (inst / n).exists()]
    if lost:
        return f"導入先({inst})のファイルが欠けています({', '.join(lost[:5])}{' ほか' if len(lost) > 5 else ''})。導入の1行をもう一度実行すると直ります"
    try:
        compile((inst / "app.py").read_text(encoding="utf-8"), "app.py", "exec")
    except (OSError, SyntaxError, ValueError) as e:
        return f"導入先({inst})の app.py が壊れています({type(e).__name__})。導入の1行をもう一度実行すると直ります"
    return ""


def launch_target(here, environ=None):
    """起動の入口がどのコピーを起動するか。返す: (導入先のフォルダ or None, 理由の文)。None のときは呼ばれたフォルダで起動する。
    振り向けない: 開発用の作業フォルダ(目印のファイル) / 番号の指定つき(同時検索・更新後の起動し直し) / FORMLIST_NO_REDIRECT / 試験中 / 導入先そのもの / 導入先が無い / 導入先が壊れている(理由つき)。
    陽性対照: 別のフォルダから起動 → 導入先を返す。原理的に取りこぼす型: このコード自体を持たない古い版のコピーを直接起動したとき(見張りと入れ替えで直す)"""
    import os
    from pathlib import Path
    env = os.environ if environ is None else environ
    here = Path(here)
    if (here / DEV_MARK).exists():
        return None, ""
    inst = installed_dir(env)
    try:
        if not (inst / "app.py").is_file() or inst.resolve() == here.resolve():
            return None, ""
    except OSError:
        return None, ""
    if env.get("FORMLIST_PORT") or env.get("FORMLIST_NO_REDIRECT") or env.get("PYTEST_CURRENT_TEST"):
        why = "FORMLIST_PORT" if env.get("FORMLIST_PORT") else ("FORMLIST_NO_REDIRECT" if env.get("FORMLIST_NO_REDIRECT") else "")
        return None, (f"環境変数 {why} が入っているため、導入先({inst})ではなく、このフォルダ({here})で起動します" if why else "")
    bad = install_broken(inst)
    if bad:
        return None, bad + f"。このフォルダ({here})で起動します"
    return inst, ""


def state(port, here):
    """'ours' = このフォルダのツールが動いている / 'other_copy' = 別のフォルダのこのツールが動いている /
    'free' = 空いている / 'foreign' = 別のアプリが使っている(またはWindowsが予約している)"""
    j = job(port)
    if j is not None:
        return "ours" if j.get("here") in (None, str(here)) else "other_copy"
    return "free" if can_bind(port) else "foreign"


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--launch-target":  # install.ps1 -Open が呼ぶ(導入先の決め方を1か所にするため)
        t, why = launch_target(sys.argv[2])
        sys.stdout.reconfigure(encoding="utf-8")
        print(str(t) if t else "")
        print(why)
