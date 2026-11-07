"""設定の文の適用で、本人が直した送信元・文面を消さないための部品(app.py の /api/sender・/api/message が使う)。Q899。
実行環境: Windows / macOS / Python 3.9+(標準ライブラリと placeholders だけ)。

決めたこと
  ・「見本のまま」と認めるのは、空 / sender.sample.json の同じ項目と一字一句同じ値 / 文面ひな形と全く同じ文面 / 無いファイルだけ。
    一部だけ直した値は(見本の【…】が残っていても)本人が直したものとして守る(破壊役 W1)。
    それ以外は「本人が直したもの」として、届いた値で上書きしない(fill_only の呼び出しのとき)。
  ・fill_only でない呼び出し(画面の保存・上書きの承認後)は上書きしてよいが、変わるファイルは先に控え(backup)を取る。
  ・控えの置き場: <ツールのフォルダ>/出力/設定の控え/<日時>/<ツールのフォルダからの相対パス>。新しい順に KEEP 個だけ残す。

陽性対照: 本人が直した文面は fill_only で skipped に回る / 見本のままの送信元の欄(【屋号】)・ひな形と同じ文面・空は入る / 控えに元の中身が残る。
誤爆の対照: 同じ値の再貼り付けは何も変えず skipped にも出さない / 控えは変わらないファイルでは作らない。
原理的に取りこぼす型: 本人が「ひな形を1文字も変えずに保存した」文面は、本人が直したものと区別できず見本として入れ替わる(控えは残る)。
  ひな形に近いが違う文面は、本人が直したものとして守る(設定の文の新しい版に入れ替えたいときは、差分を見て overwrite を承認する)。
  別のコピー(別のフォルダ)にある設定は見ない(二重コピーの検出は段階2)。
"""
import difflib
import json
import re
import shutil
import time
from pathlib import Path

import placeholders

KEEP = 50
BACKUP_NAME = "設定の控え"


def _norm(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _sample_sender():
    try:
        d = json.loads(placeholders.SAMPLE_SENDER.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def sender_fillable(current, key=None):
    """送信元の1項目が「見本のまま」か。空、または sender.sample.json の同じ項目の値と一字一句同じ(【屋号】など)だけ True。
    一部だけ直した値(署名の途中だけ直して【配信停止URL】が残っている・「【ご提案】…」のような件名)は、本人が直したものとして False
    (見本の文字が残っていても、本人の編集があるものは上書きしない。残りは送る内容を出すところで止まる)。"""
    cur = (current or "").strip()
    if not cur:
        return True
    sample = str(_sample_sender().get(key, "")).strip() if key else ""
    return bool(sample) and cur == sample


def message_fillable(current, template_text):
    """文面ファイルが「見本のまま」か。空、または文面ひな形と全く同じ(改行の違いと前後の空白は見ない)。本人が直した文面は False。"""
    cur = _norm(current)
    if not cur:
        return True
    return template_text is not None and cur == _norm(template_text)


def diff_text(current, proposed, limit=60):
    """人が読む差分(いまの値 → 届いた値)。長いときは先頭 limit 行で切る。"""
    lines = list(difflib.unified_diff((current or "").splitlines(), (proposed or "").splitlines(),
                                      "いまの値", "届いた値", lineterm="", n=1))
    if len(lines) > limit:
        lines = lines[:limit] + ["…(以降は省略。全文は項目の「いまの値」「届いた値」)"]
    return "\n".join(lines)


def backup(root, files, now=None):
    """存在するファイルを、root からの相対パスのまま <root>/出力/設定の控え/<日時>/ へ写す。写したフォルダを返す(写すものが無ければ None)。
    root: ツールのフォルダ。files: 変わる前のパスの一覧。"""
    root = Path(root)
    live = [Path(f) for f in files if Path(f).is_file()]
    if not live:
        return None
    base = root / "出力" / BACKUP_NAME
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
    dest = base / stamp
    n = 1
    while dest.exists():  # 同じ秒に2回(送信元と文面を続けて保存したとき)は枝番で分ける
        n += 1
        dest = base / f"{stamp}_{n}"
    for f in live:
        try:
            rel = f.resolve().relative_to(root.resolve())
        except ValueError:
            rel = Path(f.name)
        to = dest / rel
        to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, to)
    prune(base)
    return dest


def prune(base, keep=KEEP):
    """控えのフォルダを新しい順に keep 個だけ残す(古いものから消す。名前が日時なので名前順でよい)。"""
    try:
        dirs = sorted([p for p in Path(base).iterdir() if p.is_dir()], key=lambda p: p.name)
    except OSError:
        return
    for p in dirs[:-keep] if len(dirs) > keep else []:
        shutil.rmtree(p, ignore_errors=True)


# ---- 版の履歴(読み取り専用。Q899 第2段) ----
_VER_RE = re.compile(r"^(?:手動_)?(\d{8})_(\d{6})(?:_\d+)?$")


def _versions_dir(root):
    return Path(root) / "出力" / BACKUP_NAME


def _list_files(d):
    out = []
    try:
        for p in sorted(d.rglob("*")):
            if p.is_file() and not p.is_symlink():
                out.append(p.relative_to(d).as_posix())
    except OSError:
        pass
    return out


def history(root, limit=50):
    """控えの一覧(新しい順)。{id, at, files, manual}。中身は返さない。フォルダが無い・読めないときは空リスト。"""
    base = _versions_dir(root)
    try:
        dirs = [p for p in base.iterdir() if p.is_dir() and not p.is_symlink()]
    except OSError:
        return []
    rows = []
    for p in dirs:
        m = _VER_RE.match(p.name)
        if m:
            d, t = m.group(1), m.group(2)
            at = f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}:{t[4:]}"
        else:
            try:
                at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
            except OSError:
                at = ""
        rows.append({"id": p.name, "at": at, "files": _list_files(p), "manual": p.name.startswith("手動_")})
    rows.sort(key=lambda r: (r["at"], r["id"]), reverse=True)
    return rows[:max(0, int(limit))]


def history_item(root, version_id, rel):
    """その版の1ファイルの中身(utf-8-sig)。version_id は history() が返す名前だけ、rel は版フォルダの中に収まる相対パスだけ。外れたら None。"""
    try:
        if not isinstance(version_id, str) or not isinstance(rel, str) or not version_id or not rel:
            return None
        if version_id not in {r["id"] for r in history(root, limit=10 ** 6)}:
            return None
        if "\\" in rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in rel.split("/") or "\x00" in rel:
            return None
        vdir = (_versions_dir(root) / version_id).resolve()
        target = (vdir / rel).resolve()
        if vdir not in target.parents or not target.is_file():
            return None
        try:
            target.relative_to(Path(root).resolve())  # シンボリックリンクで root の外を読ませない
        except ValueError:
            return None
        return target.read_text(encoding="utf-8-sig")
    except (OSError, ValueError, UnicodeDecodeError):
        return None
