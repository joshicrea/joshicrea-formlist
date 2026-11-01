"""フォームとメールの両方がある会社の「どちらで送るか」の選択と、同じ会社へ二重に送らない規則。実行環境: Windows / Mac / Linux, Python 3.9+, 標準ライブラリのみ
保存先は 出力/送り方の選択.json(使う人のパソコンの中だけ。公開物には入らない)。形: {"rule": {"avoid_duplicate": true}, "choices": {"<会社の鍵>": {"channel": "form|mail", "name": "...", "at": "..."}}}
規則(avoid_duplicate)が有効(既定)のとき: 両方ある会社は、選ぶまでどちらにも送らない。片方で送ったら、もう片方へは送らない(送信の記録から自動で外す)。
無効のとき: 両方へ送れる(選択は要らない)。
フォームが一般の問い合わせ用ではない会社(採用向け・利用者向けなど=要目視)は、もともとフォームの対象外。メールがあれば、「メールで送る」を選んだときだけメールの対象に入る(規則の有効・無効に関わらず)。
原理的に取りこぼす型: 会社の鍵はHPのドメインか会社名。同じ会社が別のドメインのHPを2つ持つ・会社名が違う表記で2行に分かれる場合は、別の会社として数える。"""
import datetime as dt
import json
from pathlib import Path
from urllib.parse import urlparse

import filelock
import site_checker

CHOICE_FILE = Path(__file__).resolve().parent / "出力" / "送り方の選択.json"
CHANNELS = ("form", "mail")


def company_key(name, hp_url):
    """会社の鍵。HPのドメイン(同じ組織のサブドメインは同じ)と会社名。ドメインだけでは、共用のホスト(Googleサイト・プロバイダの個人ページ)の
    別々の会社が同じ鍵になり、選んでいない会社に送ってしまう。HPが無ければ会社名だけ"""
    nm = (name or "").strip()
    host = urlparse(hp_url or "").netloc
    if host:
        return "site:" + site_checker._root(host) + "|" + nm
    return "name:" + nm


def _read(path=None):
    p = Path(path) if path else CHOICE_FILE
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
    p = Path(path) if path else CHOICE_FILE
    with filelock.locked(p):
        data = _read(p)
        data.setdefault("rule", {})
        data.setdefault("choices", {})
        if not isinstance(data["rule"], dict):
            data["rule"] = {}
        if not isinstance(data["choices"], dict):
            data["choices"] = {}
        mutate(data)
        filelock.atomic_write(p, json.dumps(data, ensure_ascii=False, indent=1))


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
