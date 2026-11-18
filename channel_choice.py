"""フォームとメールの両方がある会社の「どちらで送るか」の選択と、同じ会社へ二重に送らない規則。実行環境: Windows / Mac / Linux, Python 3.9+, 標準ライブラリのみ
保存先は設定の置き場(settings_dir.root())の 送り方の選択.json(使う人のパソコンの中だけ。公開物には入らない。旧の 出力/送り方の選択.json は初回に1回だけ写して残す)。形: {"rule": {"avoid_duplicate": true}, "choices": {"<会社の鍵>": {"channel": "form|mail", "name": "...", "at": "..."}}}
規則(avoid_duplicate)が有効(既定)のとき: 両方ある会社は、選ぶまでどちらにも送らない。片方で送ったら、もう片方へは送らない(送信の記録から自動で外す)。
無効のとき: 両方へ送れる(選択は要らない)。
フォームが一般の問い合わせ用ではない会社(採用向け・利用者向けなど=要目視)は、もともとフォームの対象外。メールがあれば、「メールで送る」を選んだときだけメールの対象に入る(規則の有効・無効に関わらず)。
原理的に取りこぼす型: 会社の鍵はHPのドメインか会社名。同じ会社が別のドメインのHPを2つ持つ・会社名が違う表記で2行に分かれる場合は、別の会社として数える。"""
import datetime as dt
import json
from pathlib import Path
from urllib.parse import urlparse

import filelock
import settings_dir
import site_checker
import state

LEGACY_CHOICE_FILE = Path(__file__).resolve().parent / "出力" / "送り方の選択.json"  # 旧の置き場(本体のフォルダ。入れ直し・別のコピーで消える)
CHOICE_NAME = "送り方の選択.json"
CHOICE_FILE = None  # None=設定の置き場(settings_dir.root())の送り方の選択.json。試験は差し替える(波2 E)
CHANNELS = ("form", "mail")


class ChoiceSaveError(OSError):
    """送り方の選択を保存できなかった(置き場を1つにそろえられない)。旧の置き場へ書いて選択が2か所に分かれるより、保存の失敗として返す"""


def _new_file():
    """新しい置き場。None=旧の置き場のまま(試験の差し替え・設定の置き場が本体のフォルダ)"""
    if CHOICE_FILE is not None:
        return Path(CHOICE_FILE), True
    root = settings_dir.root()
    if root.resolve() == LEGACY_CHOICE_FILE.parent.parent:
        return LEGACY_CHOICE_FILE, True
    return root / CHOICE_NAME, False


def _migrate_locked(p):
    """新しい置き場 p の錠を持った状態で呼ぶ。p がまだ無く旧の置き場にあれば、1回だけ写す(旧は消さない)。失敗は OSError のまま上げる"""
    if not p.exists() and LEGACY_CHOICE_FILE.exists():
        filelock.atomic_write(p, LEGACY_CHOICE_FILE.read_text(encoding="utf-8-sig"))


def choice_file():
    """送り方の選択の置き場(読むとき)。設定の置き場にまだ無く、旧の置き場(本体の 出力/)にあれば、新しい置き場の錠の中で1回だけ写す(旧は消さない)。
    写せなかったときは、別の処理が写し終えていないかを確かめ直し、新しい置き場があればそちらを読む。無ければ旧を読む(読むだけ。書くのは _update で、旧へは書かない)。
    設定の置き場が本体のフォルダ(開発版・移行の失敗)なら旧の置き場のまま"""
    p, fixed = _new_file()
    if fixed:
        return p
    if not p.exists() and LEGACY_CHOICE_FILE.exists():
        try:
            with filelock.locked(p):
                _migrate_locked(p)
        except (OSError, TimeoutError):
            return p if p.exists() else LEGACY_CHOICE_FILE
    return p


def company_key(name, hp_url):
    """会社の鍵。HPのドメイン(同じ組織のサブドメインは同じ)と会社名。ドメインだけでは、共用のホスト(Googleサイト・プロバイダの個人ページ)の
    別々の会社が同じ鍵になり、選んでいない会社に送ってしまう。HPが無ければ会社名だけ。
    この鍵は「利用者がどの会社を選んだか」の照合に使うので、会社名は書いたまま(前後の空白だけ除く)。法人格や表記ゆれはそろえない
    (そろえると「株式会社サクラ」と「サクラ合同会社」が同じ鍵になり、選んでいない会社へ送る。2026-11-17 破壊レポート1 致命傷1-2)"""
    nm = (name or "").strip()
    host = urlparse(hp_url or "").netloc
    if host:
        return "site:" + site_checker._root(host) + "|" + nm
    return "name:" + nm


def block_key(name, hp_url):
    """同じ会社へ二重に送らないために「止める」側でだけ使う鍵。company_key と同じ形で、会社名を state.company_norm にそろえる。
    選ぶ(送る会社を決める)照合には使わない"""
    nm = state.company_norm(name)
    host = urlparse(hp_url or "").netloc
    if host:
        return "site:" + site_checker._root(host) + "|" + nm
    return "name:" + nm


def _read(path=None):
    p = Path(path) if path else choice_file()
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def rule_enabled(path=None):
    """二重送信を避ける規則。ファイルが無い・壊れている・値が真偽でないときは有効(安全側)"""
    v = (_read(path).get("rule") or {}).get("avoid_duplicate", True)
    return v if isinstance(v, bool) else True


def choices(path=None):
    c = _read(path).get("choices")
    return c if isinstance(c, dict) else {}


def get(key, path=None):
    """会社の選択。未選択・不正な値は空文字"""
    c = choices(path).get(key)
    v = c.get("channel") if isinstance(c, dict) else ""
    return v if v in CHANNELS else ""


def _update(mutate, path=None):
    """選択を書く。写し(旧→新)と書き込みを、新しい置き場の同じ錠の中で行う(別の処理の写しと重なっても選択が2か所に分かれない・波2 E)。
    写せないまま新しい置き場も無ければ、旧へは書かず ChoiceSaveError(保存の失敗)を上げる"""
    if path:
        p, fixed = Path(path), True
    else:
        p, fixed = _new_file()
    try:
        lock = filelock.locked(p)
        lock.__enter__()
    except (OSError, TimeoutError) as e:
        raise ChoiceSaveError("送り方の選択を保存できませんでした(ほかの処理が使っています)。少し待ってもう一度選んでください") from e
    try:
        if not fixed:
            try:
                _migrate_locked(p)
            except OSError as e:
                if not p.exists():
                    raise ChoiceSaveError("送り方の選択を保存できませんでした(前の選択を新しい置き場へ写せませんでした)。少し待ってもう一度選んでください") from e
        data = _read(p)
        data.setdefault("rule", {})
        data.setdefault("choices", {})
        if not isinstance(data["rule"], dict):
            data["rule"] = {}
        if not isinstance(data["choices"], dict):
            data["choices"] = {}
        mutate(data)
        filelock.atomic_write(p, json.dumps(data, ensure_ascii=False, indent=1))
    finally:
        lock.__exit__(None, None, None)


def set_choice(key, channel, name="", path=None):
    """選択を保存する。channel が空なら選択を外す"""
    if channel and channel not in CHANNELS:
        raise ValueError("channel は form か mail です")

    def m(d):
        if channel:
            d["choices"][key] = {"channel": channel, "name": name, "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
        else:
            d["choices"].pop(key, None)
    _update(m, path)


def set_rule(enabled, path=None):
    _update(lambda d: d["rule"].__setitem__("avoid_duplicate", bool(enabled)), path)


def allowed_channels(has_general_form, has_mail, key, path=None):
    """この会社に送ってよいチャネルの集合。
    has_general_form: 一般の問い合わせフォームがある(判定がフォームあり)。has_mail: 送れる形のアドレスがある。
    フォームだけ・メールだけの会社は、それぞれ自分のチャネルだけ(選択は要らない)"""
    if has_general_form and not has_mail:
        return {"form"}
    if has_mail and not has_general_form:
        return {"mail"}
    if not has_general_form and not has_mail:
        return set()
    if not rule_enabled(path):
        return {"form", "mail"}
    c = get(key, path)
    return {c} if c else set()
