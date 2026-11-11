"""使う AI（エンジン）の設定。実行環境: Windows / Mac / Linux（Python 3.9+）。

設定ファイルは AI設定.json で、送信元・文面と同じ利用者領域（settings_dir の root）に置く。
中身は {"engine": "claude_code"} だけ（値は claude_code / muse の2つ。将来 Codex を
画面の選択肢に足す場合も同じ欄で扱う）。送信元の本人情報とは性質が違うので sender.json には混ぜない。

読み取りの作法（設計の安全弁）:
  ファイルが無い・壊れている・値が想定外のときは、黙って muse 側に倒さず、既定の claude_code
  として動き、理由（note）を返す。画面は note をそのまま利用者に出す。
保存の作法:
  変わる前のファイルは settings_guard.backup で控えを残してから書く（送信元・文面と同じ）。
  計測キーなどの秘密はこのファイルに書かない（接続の補助情報もここには持たせない。
  接続の履歴はログと /api/whoami の状態で担う）。
"""
import json
import os
from pathlib import Path

import settings_dir
import settings_guard

FILE_NAME = "AI設定.json"
ENGINES = ("claude_code", "muse")
DEFAULT = "claude_code"


def file_path(root=None):
    """AI設定.json の置き場所。root は設定の置き場（省略時は settings_dir.root()）"""
    base = Path(root) if root is not None else settings_dir.root()
    return base / FILE_NAME


def load(root=None):
    """設定を読む。戻りは {"engine": 値, "ok": 読めたか, "note": 利用者に出す理由（ok のときは空）}。
    どんな壊れ方でも例外にせず、既定値と理由を返す（呼び出し側は note を画面に出す）。"""
    p = file_path(root)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {"engine": DEFAULT, "ok": False, "note": "AI の設定がまだ無いので、Claude Code で使う設定になっています"}
    except OSError:
        return {"engine": DEFAULT, "ok": False, "note": "AI の設定が読めなかったので、Claude Code で使う設定に戻しました"}
    try:
        d = json.loads(text)
    except ValueError:
        return {"engine": DEFAULT, "ok": False, "note": "AI の設定が壊れていたので、Claude Code で使う設定に戻しました"}
    v = d.get("engine") if isinstance(d, dict) else None
    if v not in ENGINES:
        return {"engine": DEFAULT, "ok": False, "note": "AI の設定の値が想定と違ったので、Claude Code で使う設定に戻しました"}
    return {"engine": v, "ok": True, "note": ""}


def engine(root=None):
    """いま選ばれているエンジン（文字列だけ要るとき）"""
    return load(root)["engine"]


def save(value, root=None):
    """エンジンを保存する。値が想定外なら ValueError（呼び出し側が利用者向けの文で返す）。
    変わる前のファイルは settings_guard で控えを残す。戻りは控えのパス（無ければ None）。"""
    if value not in ENGINES:
        raise ValueError("engine は claude_code か muse です")
    p = file_path(root)
    text = json.dumps({"engine": value}, ensure_ascii=False, indent=1)
    kept = None
    if p.exists():
        try:
            same = p.read_text(encoding="utf-8-sig").strip() == text.strip()
        except (OSError, ValueError):  # 壊れて読めないものも、上書きの前に控える
            same = False
        if not same:
            kept = settings_guard.backup(p.parent, [p])  # 変わる前の設定を控える（送信元・文面と同じ作法）
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)  # 書いている途中で落ちても、半端なファイルが残らない
    return kept
