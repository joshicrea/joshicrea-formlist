"""本番送信の「人の承認」と、使い切りの送信チケット。実行環境: Windows / Python 3.9+
  ・ask_human: 画面の外(OSの確認ダイアログ)で人に「はい/いいえ」を聞く。HTTPでは押せない
  ・issue_ticket / redeem: 画面(app.py)のプロセスが、人の承認のあとでメモリにだけ持つ使い切りの鍵。ファイルには置かない
  ・consume_ticket: 送信を受け持つ autosend.py が、起動元の画面に問い合わせて鍵を使い切る。画面が発行していない鍵は通らない
守備範囲: 防ぐのは「HTTPで承認を偽造する」「autosend.py を直接動かす・チケットのファイルを書いて関門を飛ばす」。
          防げないもの: 画面操作のできるAIがダイアログを押すこと。同じWindows利用者のプロセスが、このツールのコードを書き換えるか、
          画面の偽物を立てるか、自前のブラウザ操作で送ること(Claude Code のようにコマンドを実行できるAIは、許可の設定と手引きで止める)。
"""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path


TICKET_ENV = "FORMLIST_SEND_TOKEN"
PORT_ENV = "FORMLIST_APP_PORT"  # 子プロセスが鍵を問い合わせる先(起動元の画面のポート)
UI_TOKEN_ENV = "FORMLIST_UI_TOKEN"  # 画面の操作用の合言葉。問い合わせも画面と同じ関門を通る
TICKET_SECONDS = 600
ASKER = None  # テストが差し替える。(題, 本文) -> bool


def ask_human(title, body):
    """はい=True。出せない環境・閉じた・いいえ は False(安全側)"""
    if ASKER is not None:
        return bool(ASKER(title, body))
    try:
        if sys.platform == "win32":
            import ctypes
            MB_YESNO, MB_ICONQUESTION, MB_DEFBUTTON2, MB_TOPMOST, MB_SETFOREGROUND = 0x4, 0x20, 0x100, 0x40000, 0x10000
            r = ctypes.windll.user32.MessageBoxW(None, body, title, MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2 | MB_TOPMOST | MB_SETFOREGROUND)
            return r == 6  # IDYES
        if sys.platform == "darwin":
            script = 'display dialog %s with title %s buttons {"やめる","送ることを承認する"} default button "やめる"' % (json.dumps(body, ensure_ascii=False), json.dumps(title, ensure_ascii=False))
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
            return r.returncode == 0 and "送ることを承認する" in r.stdout
    except Exception:
        return False
    return False


_TICKETS = {}  # sha256(鍵) -> (確認番号, 期限)。画面のプロセスのメモリにだけある
_LOCK = threading.Lock()


def issue_ticket(expect):
    """承認のあとで1回だけ発行する(画面のプロセスで呼ぶ)。戻り値の token を子プロセス(autosend)の環境変数で渡す"""
    token = secrets.token_urlsafe(24)
    with _LOCK:
        now = time.time()
        for h in [h for h, (_e, until) in _TICKETS.items() if until <= now]:
            del _TICKETS[h]
        _TICKETS[hashlib.sha256(token.encode()).hexdigest()] = (expect, now + TICKET_SECONDS)
    return token


def redeem(token, expect):
    """画面のプロセスの中で照合する。合えば True を返してその場で消す(使い切り)。期限切れ・不一致・無しは False"""
    if not token or not expect:
        return False
    h = hashlib.sha256(str(token).encode()).hexdigest()
    with _LOCK:
        t = _TICKETS.get(h)
        if not t or t[0] != expect:
            return False
        del _TICKETS[h]
        return t[1] > time.time()


def consume_ticket(token, expect):
    """autosend 側で呼ぶ。起動元の画面(環境変数のポート)に問い合わせて使い切る。画面の外から起動したときは、
    このプロセスのメモリを見る(別のプロセスが発行した鍵は無いので通らない)"""
    if not token or not expect:
        return False
    port = os.environ.get(PORT_ENV, "")
    if not port.isdigit():
        return redeem(token, expect)
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ticket/consume", data=json.dumps({"token": token, "expect": expect}).encode(),
                                 headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{port}", "X-Token": os.environ.get(UI_TOKEN_ENV, "")}, method="POST")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def revoke_ticket():
    with _LOCK:
        _TICKETS.clear()


def env_token():
    return os.environ.get(TICKET_ENV, "")
