"""画面のサーバー(app.py)を置く番号と、その番号の使われ方の判定。画面(app.py)・MCP(mcp_server.py)・導入(install.ps1)が同じ定義を使う。実行環境: Windows / Python 3.9+。
1つのサーバーは同時に1つの「探す」しか動かさない。別の条件で同時に探すときは、別の番号でもう1つサーバーを立てる(最大3つ)。
3つまでにしているのは、1つのサーバーが最大3本のブラウザを動かすため、増やすとパソコンが重くなり、Googleにも止められやすくなるから。
画面の入口は 8780 に固定する(ブックマークしても変わらない)。8781・8782 は、同時に探すときの追加の画面。"""
import json
import socket
import urllib.request

POOL = [8780, 8781, 8782]
MAIN = POOL[0]
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


def state(port, here):
    """'ours' = このフォルダのツールが動いている / 'other_copy' = 別のフォルダのこのツールが動いている /
    'free' = 空いている / 'foreign' = 別のアプリが使っている(またはWindowsが予約している)"""
    j = job(port)
    if j is not None:
        return "ours" if j.get("here") in (None, str(here)) else "other_copy"
    return "free" if can_bind(port) else "foreign"
