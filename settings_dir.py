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
PIN_ENV = "FORMLIST_SETTINGS_FAILED_HERE"  # 移行に失敗して本体のフォルダで動いているコピーのフォルダ。同じフォルダの子プロセスだけが読む
LOCK_NAME = ".移行"  # 移行の排他(filelock の <名前>.lock)。同時に起動した2つのコピーの移行が混ざらない(F3)
LOCK_WAIT = 120
PENDING = ".写しかけ_{}.json"  # まだ済んでいない移行で、このコピーが置き場へ写したファイルとその SHA256(F2)
STALE_SECONDS = 2  # 本体のファイルが置き場より「この秒数を超えて新しい」ときだけ、移行後の保存とみなす(コピー時刻の丸め・同期の揺れを誤報しない)


def _norm(p):
    try:
        return os.path.normcase(str(Path(p).resolve()))
    except OSError:
        return os.path.normcase(str(p))


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


def resolve(here=None, environ=None, retry=False):
    """設定の置き場を決める。返す: {"root": Path, "relocated": bool, "fallback_reason": str|None, "why": str}
    relocated=False は、ツール本体のフォルダ(従来の置き場)を使うこと。
    retry=True は起動時の setup() から: 移行に失敗した印(PIN_ENV)を無視して、もう一度置き場を決める(次の起動でやり直す)。"""
    here = Path(here) if here else HERE
    env = os.environ if environ is None else environ
    pin = env.get(PIN_ENV)
    if pin and not retry and _norm(pin) == _norm(here):
        # 親(同じフォルダの画面)が移行に失敗して本体のフォルダで動いている。送る処理などの子も同じ本体のフォルダを読む。
        # 印は「このフォルダ」にだけ効く。別のコピー(導入先への振り向け先など)は印を無視して自分で決める(2026-11-10 破壊検証 F1)
        return {"root": here, "relocated": False, "fallback_reason": "親が設定の移行に失敗したため本体のフォルダで動く", "why": "移行に失敗した親と同じ置き場"}
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


def _sha(p: Path):
    import hashlib
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def migrate(here, root, now=None):
    """here(ツール本体)の設定のうち root に無いものを写す。元は消さない。返す: {"copied": [相対パス], "conflicts": [相対パス], "errors": [文]}。
    root が here と同じなら何もしない。記録を root/移行の記録.json に追記する(何も起きなかった実行は書かない)。"""
    here, root = Path(here), Path(root)
    res = {"copied": [], "conflicts": [], "errors": []}
    if here.resolve() == root.resolve():
        return res
    # 同時に起動した2つのコピーが同じ置き場へ写すと、ファイルごとに A 由来と B 由来が混ざる(F3)。移行の全体を1つの排他の中で行う。
    # 後から来たコピーは先の移行が終わるまで待ち、終わった置き場(済みの印つき)を見てから自分の分を判断する
    try:
        root.mkdir(parents=True, exist_ok=True)
        import filelock
        with filelock.locked(root / LOCK_NAME, timeout=LOCK_WAIT):
            return _migrate_locked(here, root, res, now)
    except (OSError, TimeoutError, ImportError) as e:
        res["errors"].append(f"移行の排他: {type(e).__name__}")
        return res


def _pending_path(root, here):
    import hashlib
    return Path(root) / PENDING.format(hashlib.sha1(_norm(here).encode("utf-8")).hexdigest()[:12])


def _migrate_locked(here, root, res, now):
    # 前の実行が強制終了で残した一時ファイルを片付ける(排他の中なので、写している最中の別の実行は無い)
    temps = list(root.glob("*.移行中*"))
    for d in DIRS:
        if (root / d).is_dir():
            temps += list((root / d).rglob("*.移行中*"))
    for t in temps:
        try:
            if t.is_file():
                t.unlink()
        except OSError:
            pass
    # 移行は、そのコピー(here)から1回だけ。済んだ後に本人が置き場で設定を直すと、本体側の古い元と必ず違うので、毎回「衝突」と誤報しないため
    marker = root / MARKER
    try:
        done = json.loads(marker.read_text(encoding="utf-8-sig"))
        done = done if isinstance(done, list) else []
    except (OSError, ValueError):
        done = []
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
    if str(here.resolve()) in done:
        # 移行済みでも、置き場が使えず本体のフォルダで動いた間の保存は、置き場より新しい。見せないと、直した文面が黙って古い置き場の内容に戻って見える
        for rel in srcs:
            s, d = here / rel, root / rel
            try:
                if d.is_file() and s.stat().st_mtime > d.stat().st_mtime + STALE_SECONDS and not _same(s, d):
                    res["conflicts"].append(rel.as_posix())
            except OSError:
                pass
        return res
    # 前の実行(失敗して本体のフォルダで動いた)がこのコピーから写しかけたファイル。置き場で誰も直していなければ(写したときの SHA256 のまま)、
    # 元(本体のフォルダ)が正: 失敗の間に本体で直した内容で写し直し、本体で消したものは置き場からも消す(F2。衝突として除外して古い内容へ切り替えない)
    pend_path = _pending_path(root, here)
    try:
        pending = json.loads(pend_path.read_text(encoding="utf-8"))
        pending = pending if isinstance(pending, dict) else {}
    except (OSError, ValueError):
        pending = {}

    def save_pending():
        pend_path.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")

    names = {rel.as_posix() for rel in srcs}
    for relp, sha in list(pending.items()):
        if relp in names:
            continue
        d = root / relp
        try:
            if d.is_file() and _sha(d) == sha:
                d.unlink()
                res["copied"].append(f"{relp}(本体で消えたので置き場からも消した)")
            pending.pop(relp)
            save_pending()
        except OSError as e:
            res["errors"].append(f"{relp}: 写しかけの片付け({type(e).__name__})")
    for rel in srcs:
        s, d = here / rel, root / rel
        relp = rel.as_posix()
        try:
            ours = d.is_file() and relp in pending and _sha(d) == pending[relp]
            if not d.exists() or (ours and not _same(s, d)):
                # 複製→中身の照合→名前を直す(R2・R16)。途中で止まっても、半端なファイルが本来の名前で残らない。
                # 一時ファイルの名前はプロセスごとに変える(同時に写す別のプロセスと同じ一時ファイルを奪い合わない・F3)
                d.parent.mkdir(parents=True, exist_ok=True)
                tmp = d.with_name(f"{d.name}.移行中.{os.getpid()}.{uuid.uuid4().hex[:8]}")
                shutil.copy2(s, tmp)
                sha = _sha(s)
                if _sha(tmp) != sha:
                    tmp.unlink()
                    raise OSError("複製の中身が元と一致しない")
                pending[relp] = sha  # 名前を直す前に記録する(直した直後に止まっても、次の実行が自分の写しだと分かる)
                save_pending()
                os.replace(tmp, d)
                res["copied"].append(relp)
            elif not _same(s, d):
                res["conflicts"].append(relp)
        except OSError as e:
            res["errors"].append(f"{relp}: {type(e).__name__}")
    # 照合: 元の全ファイルが、置き場に同じ中身であるか(衝突として残したものを除く)。1件でも欠けていれば移行済みにしない(R2)
    res["checked"] = 0
    for rel in srcs:
        if rel.as_posix() in res["conflicts"]:
            continue
        try:
            if _sha(root / rel) != _sha(here / rel):
                res["errors"].append(f"{rel.as_posix()}: 照合で不一致")
            else:
                res["checked"] += 1
        except OSError as e:
            res["errors"].append(f"{rel.as_posix()}: 照合できない({type(e).__name__})")
    # 第1段の控え(本体の 出力/設定の控え)を、置き場の控えへ写す。写さないと、版の履歴に第1段の控えが出ない(上書き前の文面を戻せない)
    old_base = here / "出力" / "設定の控え"
    if old_base.is_dir():
        for v in sorted(p for p in old_base.iterdir() if p.is_dir() and not p.is_symlink()):
            to = root / "出力" / "設定の控え" / v.name
            if to.exists():
                continue
            try:
                shutil.copytree(v, to, symlinks=False)
                res["copied"].append(f"出力/設定の控え/{v.name}")
            except (OSError, shutil.Error) as e:
                shutil.rmtree(to, ignore_errors=True)  # 途中までの写しを残さない(残すと次の起動で「ある」とみなして、欠けた控えのままになる)
                res["errors"].append(f"出力/設定の控え/{v.name}: {type(e).__name__}")
    if not res["errors"]:  # 1つでも失敗したら済みにしない(次の起動でやり直す)
        try:
            marker.write_text(json.dumps(done + [str(here.resolve())], ensure_ascii=False), encoding="utf-8")
            if pend_path.exists():
                pend_path.unlink()  # 済んだので、写しかけの記録は要らない(以後は置き場が正)
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


def _has_settings(here):
    here = Path(here)
    return any((here / n).is_file() for n in ITEMS) or any((here / d).is_dir() and any((here / d).rglob("*")) for d in DIRS)


def setup(here=None, environ=None):
    """起動時に1回呼ぶ。置き場を決めて、移行する。返す: resolve() の結果 + {"migration": migrate() の結果}。
    環境変数を渡さない呼び出しの結果は、root() が使い回す(置き場の判定と書き込み確認を何度も行わないため)。"""
    here = Path(here) if here else HERE
    # import 時に root() が決めた置き場と食い違わないよう、環境を渡さない呼び出しは決め直さず使い回す
    cached = _CACHE.get("resolve")
    if environ is None and cached and Path(cached.get("here", here)) == here and cached.get("why") != "移行に失敗した親と同じ置き場":
        r = dict(cached)
    else:
        r = resolve(here, environ, retry=True)  # 起動のたびに、前回の失敗の印を無視してやり直す(更新後の自動の起動し直しでも再試行される)
    r["here"] = here
    r["migration"] = {"copied": [], "conflicts": [], "errors": []}
    if r["relocated"]:
        inst = _installed_first(here, environ)
        if inst:
            # 導入先のコピー(正規の最新版。MCP も画面も以後はここを使う)の設定を先に写す。古いコピーを先にダブルクリックしても、
            # 古い設定が置き場を先取りしない(2026-11-10 破壊検証 W1/E4)。導入先が同じ名前で持つものは、後から写すコピーでは「衝突」になる
            mi = migrate(inst, r["root"])
            r["migration_installed"] = {"from": str(inst), **mi}
            r["migration"]["errors"] += [f"導入先 {e}" for e in mi["errors"]]
        mh = migrate(here, r["root"])
        for k in ("copied", "conflicts", "errors"):
            r["migration"][k] += mh.get(k, [])
        if "checked" in mh:
            r["migration"]["checked"] = mh["checked"]
    if r["relocated"] and r["migration"]["errors"] and _has_settings(here):
        # 複製か照合が1件でも失敗したら、読み先を切り替えない。元(本体のフォルダ)を読み続け、次の起動でやり直す(R2)。
        # 本体のフォルダに設定が無い(新規の導入)ときは、失うものが無いので置き場のまま
        r.update(root=here, relocated=False, why="移行の照合に失敗したので本体のフォルダで動く",
                 fallback_reason="設定の移行で、複製か照合に失敗しました(" + "、".join(r["migration"]["errors"][:3]) + ")。このコピーだけ、元の場所の設定で動いています。次の起動でやり直します")
        if environ is None:
            _CACHE["resolve"] = {k: r[k] for k in ("root", "relocated", "fallback_reason", "why")}
            # 送る処理などの子プロセスも本体のフォルダの設定を読む。ただし FORMLIST_SETTINGS_DIR には書かない: それは別のコピー
            # (導入先への振り向け先・更新後の起動し直し)にも引き継がれ、別のコピーがこのコピーのフォルダを読んでしまう(F1)。
            # 印はこのフォルダにだけ効き(resolve の PIN_ENV)、起動時の setup() は印を無視してやり直す
            os.environ[PIN_ENV] = str(here)
            old = os.environ.get("FORMLIST_SETTINGS_DIR")
            if old and _norm(old) == _norm(here):
                os.environ.pop("FORMLIST_SETTINGS_DIR")  # 古い版がこの方式で書いた値を、子と別のコピーへ渡さない
    if environ is None:
        _CACHE["setup"] = r
        if r["relocated"] and not r["fallback_reason"]:
            os.environ.pop(PIN_ENV, None)  # 移行が済んだので、前の失敗の印を子へ渡さない
            # 送る処理などの子プロセスが、親と同じ置き場を使う(子は setup() を通らず root() で自分で決めるため)
            os.environ.update(FORMLIST_SETTINGS_DIR=str(r["root"]))  # 自分の変数への書き込み(外部資産への依存ではない)
    return r


def _installed_first(here, environ=None):
    """先に写す導入先のフォルダ。対象外なら None。
    対象: 置き場が利用者領域(環境変数で指定されていない)か、導入先が環境変数 FORMLIST_INSTALL_DIR で明示されている(試験)。
    導入先がこのフォルダ自身・無い・壊れている(必須ファイルの欠け)ときは対象外"""
    env = os.environ if environ is None else environ
    if not env.get("FORMLIST_INSTALL_DIR") and (environ is not None or os.environ.get("PYTEST_CURRENT_TEST")):
        # 環境を渡した呼び出し(試験)と試験の実行中は、導入先が明示されたときだけ。既定の導入先(このPCの本物の設定)を試験の置き場へ写さない
        # (2026-11-10 実装中に1回起きた: 試験の一時フォルダへこのPCの導入先の送信元が写った。一時フォルダは削除済み)
        return None
    forced = env.get("FORMLIST_SETTINGS_DIR")
    if forced and _norm(forced) != _norm(_user_root(env)) and not env.get("FORMLIST_INSTALL_DIR"):
        return None  # 試験・運用者が別の置き場を指定した: その環境の実際の導入先(本物の設定)を読みにいかない
    try:
        import ports
        inst = ports.installed_dir(env)
        if not inst.is_dir() or _norm(inst) == _norm(here) or ports.install_broken(inst):
            return None
        return inst
    except Exception:
        return None


def root():
    """設定の置き場(Path)。setup() 済みならその結果、まだなら判定だけ(移行はしない)。app.py 以外のモジュールの定義が使う。"""
    r = _CACHE.get("setup") or _CACHE.get("resolve")
    if r is None:
        r = _CACHE["resolve"] = resolve()
    return Path(r["root"])
