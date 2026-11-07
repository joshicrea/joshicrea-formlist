"""ツールのコピーが2つあるとき、画面が古いコピーの設定を出していないかを調べる部品。Q899 第2段。実行環境: Windows / macOS / Linux / Python 3.9+(標準ライブラリだけ)。

読み取りだけ: 何も変えない・書かない。他のポートへ http://127.0.0.1:<port>/api/whoami(トークン不要)を0.5秒の待ちで1回読む。
返す辞書は画面(META.copy)と手順書が読む固定の形: here / settings_dir / settings_fallback / msg_dir / msg_mtime / other_copies / conflicts / warnings。

陽性対照: 別のコピーがポートを握る→ other_copies と warning が出る / 置き場の衝突→ warning / 置き場に作れず本体のフォルダ→ warning。
誤爆の対照: 自分だけ→ warnings 空 / 応答しないポート・壊れたJSON・ツールでない応答→ 無かったものとして出さない / 自分のポートは除く。
原理的に取りこぼす型: 応答が0.5秒を超える忙しい別コピー(読めず無かったものになる)。whoami を持たない旧版のコピー(古い版ほど見えない)。
  127.0.0.1 以外で動く別コピー。ポート(POOL)の外で動くコピー。ツールでない別のプログラムが同じポートを握る場合(警告は出さず、画面が出ないことで気づく)。
"""
import json
import urllib.request
from pathlib import Path

import ports

TIMEOUT = 0.5


def _whoami(port):
    """port の /api/whoami を読む。読めない・壊れている・ツールでない応答は None。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/whoami", timeout=TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8-sig"))
    except Exception:
        return None
    return d if isinstance(d, dict) and d.get("tool") == "formlist" else None


def _newest_txt(msg_dir):
    try:
        ts = [p.stat().st_mtime for p in Path(msg_dir).rglob("*.txt") if p.is_file()]
    except OSError:
        return None
    if not ts:
        return None
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(max(ts)))


def status(here, settings, ports=ports.POOL, whoami=None, install_default=None):
    here = Path(here)
    root = Path(settings["root"])
    mig = settings.get("migration") or {}
    fallback = settings.get("fallback_reason")
    ask = whoami or _whoami
    msg_dir = root / "文面"
    others = []
    for port in ports:
        d = ask(port)
        if not isinstance(d, dict):
            continue
        there = str(d.get("here") or "")
        if not there or Path(there) == here:  # 自分のコピー(同じフォルダ)は除く
            continue
        others.append({"here": there, "port": int(d.get("port") or port), "version": str(d.get("version") or "")})
    conflicts = list(mig.get("conflicts") or [])
    warnings = [f"別のコピー({o['here']})が画面の入口({o['port']}番)を握っています。そちらの設定が表示されている可能性があります" for o in others]
    if conflicts:
        warnings.append(f"設定の置き場に、このコピーと違う内容のファイルがあります({len(conflicts)}件)。どちらが新しいか確かめてください")
    if fallback:
        warnings.append(f"設定の保存先を利用者の領域に作れなかったため、ツールのフォルダに保存しています(理由: {fallback})")
    return {"here": str(here), "settings_dir": str(root), "settings_fallback": fallback or None,
            "msg_dir": str(msg_dir), "msg_mtime": _newest_txt(msg_dir), "other_copies": others,
            "conflicts": conflicts, "warnings": warnings}
