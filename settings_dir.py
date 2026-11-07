"""本人の設定(送信元・文面・採用記録・計測キー)の置き場を1か所で決める部品。Q899 第2段。実行環境: Windows / macOS / Linux / Python 3.9+(標準ライブラリだけ)。

なぜ分けるか: 設定をツール本体のフォルダ(app/)に置くと、ツールのコピーが2つあるとき・入れ直したとき・更新したときに、
どのコピーの設定が使われるかが変わり、設定した文面が消えて古い文面が出る(2026-11-07 依頼主の事例)。
利用者領域(%LOCALAPPDATA%\\FormListTool\\設定)に置けば、コピーが何個あっても設定は1つになる。

置き場の決め方(上から。最初に決まったもの):
  1. 環境変数 FORMLIST_SETTINGS_DIR(試験・運用者の指定)
  2. 試験の実行中(環境変数 PYTEST_CURRENT_TEST)と、開発用の作業フォルダ(開発版_自動更新しない.txt がある): ツール本体のフォルダ(従来のまま)
  3. Windows: %LOCALAPPDATA%\\FormListTool\\設定 / macOS: ~/Library/Application Support/FormListTool/設定 / 他: ~/.local/share/FormListTool/設定
  作れない・書けないときは、ツール本体のフォルダに戻る(fallback=True。画面が警告できるよう、理由を返す)。
移行: 初回に、ツール本体のフォルダにあって新しい置き場に「まだ無い」ものだけを写す。元は消さない。
  新しい置き場に既にあって中身が違うものは、上書きせず「衝突」として記録する(別のコピーが先に移していた・本人が新しい場所で直した、のどちらもありうるため)。
  移行の記録は 新しい置き場/移行の記録.json(どのコピーから・いつ・何を写したか・衝突)。

陽性対照: 空の置き場へ写る(送信元・文面の全ファイル・採用記録・計測キー) / 2回目は何も写さず何も壊さない / 衝突は上書きされず記録される / 途中で書けなくなれば本体のフォルダで動く。
誤爆の対照: 既にある設定を、古いコピーの設定で上書きしない。移行の元(本体のフォルダ)を消さない。
原理的に取りこぼす型: 新しい置き場に既にある設定が「古い」のか「新しい」のかは決められない(衝突として見せて人が選ぶ)。
  別のユーザーアカウントの設定は見ない。OneDrive が %LOCALAPPDATA% を同期する構成は見ない。
"""
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEV_MARK = "開発版_自動更新しない.txt"
ITEMS = ("sender.json", "message_adoptions.json", "計測キー.txt")  # 設定のファイル
DIRS = ("文面",)  # 設定のフォルダ(中のパターンも含め、ファイル単位で写す)
RECORD = "移行の記録.json"
MARKER = ".移行済み.json"  # 移行を済ませたコピー(フォルダ)の一覧


def _user_root(env=None):
    """利用者領域の既定の置き場(まだ作らない)。env は resolve に渡された環境(省略時は実環境)。"""
    env = os.environ if env is None else env
    if sys.platform == "win32":
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "FormListTool" / "設定"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FormListTool" / "設定"
    return Path.home() / ".local" / "share" / "FormListTool" / "設定"


def _writable(p: Path):
    try:
        p.mkdir(parents=True, exist_ok=True)
        # 作業ファイルの名前はプロセスごとに変える(同時に判定した別のプロセスに消されて「書けない」と誤判定し、古い置き場へ戻らないため)
        probe = p / f".書き込み確認_{os.getpid()}_{uuid.uuid4().hex}"  # 時刻は Windows では粗く、同じ値になりうる
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return None
    except OSError as e:
        return f"{type(e).__name__}: {e}"


def resolve(here=None, environ=None):
    """設定の置き場を決める。返す: {"root": Path, "relocated": bool, "fallback_reason": str|None, "why": str}
    relocated=False は、ツール本体のフォルダ(従来の置き場)を使うこと。"""
    here = Path(here) if here else HERE
    env = os.environ if environ is None else environ
    forced = env.get("FORMLIST_SETTINGS_DIR")
    if forced:
        r = Path(forced)
        err = _writable(r)
        if err:
            return {"root": here, "relocated": False, "fallback_reason": err, "why": "環境変数の指定が使えない"}
        return {"root": r, "relocated": r.resolve() != here.resolve(), "fallback_reason": None, "why": "環境変数の指定"}
    if env.get("PYTEST_CURRENT_TEST"):
        return {"root": here, "relocated": False, "fallback_reason": None, "why": "試験中は従来の置き場"}
    if (here / DEV_MARK).exists():
        return {"root": here, "relocated": False, "fallback_reason": None, "why": "開発用の作業フォルダ"}
    r = _user_root(env)
    err = _writable(r)
    if err:
        return {"root": here, "relocated": False, "fallback_reason": err, "why": "利用者領域に作れないので本体のフォルダで動く"}
    return {"root": r, "relocated": True, "fallback_reason": None, "why": "利用者領域"}


def _same(a: Path, b: Path):
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def migrate(here, root, now=None):
    """here(ツール本体)の設定のうち root に無いものを写す。元は消さない。返す: {"copied": [相対パス], "conflicts": [相対パス], "errors": [文]}。
    root が here と同じなら何もしない。記録を root/移行の記録.json に追記する(何も起きなかった実行は書かない)。"""
    here, root = Path(here), Path(root)
    res = {"copied": [], "conflicts": [], "errors": []}
    if here.resolve() == root.resolve():
        return res
    # 移行は、そのコピー(here)から1回だけ。済んだ後に本人が置き場で設定を直すと、本体側の古い元と必ず違うので、毎回「衝突」と誤報しないため
    marker = root / MARKER
    try:
        done = json.loads(marker.read_text(encoding="utf-8-sig"))
        done = done if isinstance(done, list) else []
    except (OSError, ValueError):
        done = []
    if str(here.resolve()) in done:
        return res
    srcs = []
    for n in ITEMS:
        if (here / n).is_file():
            srcs.append(Path(n))
    for d in DIRS:
        base = here / d
        if base.is_dir():
            for f in sorted(base.rglob("*")):
                rel = f.relative_to(here)
                if f.is_file() and "_削除済み" not in rel.parts and ".." not in rel.parts:
                    srcs.append(rel)
    for rel in srcs:
        s, d = here / rel, root / rel
        try:
            if not d.exists():
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)
                res["copied"].append(rel.as_posix())
            elif not _same(s, d):
                res["conflicts"].append(rel.as_posix())
        except OSError as e:
            res["errors"].append(f"{rel.as_posix()}: {type(e).__name__}")
    if not res["errors"]:  # 1つでも失敗したら済みにしない(次の起動でやり直す)
        try:
            marker.write_text(json.dumps(done + [str(here.resolve())], ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            res["errors"].append(f"{MARKER}: {type(e).__name__}")
    if res["copied"] or res["conflicts"] or res["errors"]:
        rec_path = root / RECORD
        try:
            hist = json.loads(rec_path.read_text(encoding="utf-8-sig"))
            hist = hist if isinstance(hist, list) else []
        except (OSError, ValueError):
            hist = []
        hist.append({"at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "from": str(here), **res})
        try:
            rec_path.write_text(json.dumps(hist[-50:], ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            res["errors"].append(f"{RECORD}: {type(e).__name__}")
    return res


def read_record(root):
    """移行の記録(新しい順に最大50件)。無い・壊れていれば空。画面と手順書が使う。"""
    try:
        hist = json.loads((Path(root) / RECORD).read_text(encoding="utf-8-sig"))
        return list(reversed(hist)) if isinstance(hist, list) else []
    except (OSError, ValueError):
        return []


_CACHE = {}


def setup(here=None, environ=None):
    """起動時に1回呼ぶ。置き場を決めて、移行する。返す: resolve() の結果 + {"migration": migrate() の結果}。
    環境変数を渡さない呼び出しの結果は、root() が使い回す(置き場の判定と書き込み確認を何度も行わないため)。"""
    here = Path(here) if here else HERE
    # import 時に root() が決めた置き場と食い違わないよう、環境を渡さない呼び出しは決め直さず使い回す
    r = dict(_CACHE["resolve"]) if environ is None and _CACHE.get("resolve") and Path(_CACHE["resolve"].get("here", here)) == here else resolve(here, environ)
    r["here"] = here
    r["migration"] = migrate(here, r["root"]) if r["relocated"] else {"copied": [], "conflicts": [], "errors": []}
    if environ is None:
        _CACHE["setup"] = r
        if r["relocated"] and not r["fallback_reason"]:
            # 送る処理などの子プロセスが、親と同じ置き場を使う(子は setup() を通らず root() で自分で決めるため)
            os.environ.update(FORMLIST_SETTINGS_DIR=str(r["root"]))  # 自分の変数への書き込み(外部資産への依存ではない)
    return r


def root():
    """設定の置き場(Path)。setup() 済みならその結果、まだなら判定だけ(移行はしない)。app.py 以外のモジュールの定義が使う。"""
    r = _CACHE.get("setup") or _CACHE.get("resolve")
    if r is None:
        r = _CACHE["resolve"] = resolve()
    return Path(r["root"])
