"""NGリスト(送らない会社)の自動登録と判定。出力/NGリスト.txt に1行1件で持つ。
  ドメイン  # 理由        … そのドメインの会社を全て除く(営業お断りの表示があった会社)
  @フォームURL  # 理由    … そのフォーム1つだけを除く(自動入力では送れないと分かったフォーム)
自動で送れない判定は「ページに captcha という文字がある」程度の弱い根拠なので、法人ドメイン全体は消さない。
タイムアウトなど一時的な失敗は入れない。戻したいときは、その行を消す。
"""
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import filelock

# 複数の会社が使う問い合わせサービス。ここをドメインでNGに入れると無関係の会社まで消える
# パスで店・施設を分けるサービス。ドメインで入れると、そのサービス上の無関係の店まで消える
PATH_HOSTED = ("sites.google.com", "peraichi.com", "instagram.com", "lit.link", "jalan.net", "ameblo.jp", "note.com", "facebook.com", "x.com", "twitter.com")
SHARED = ("forms.gle", "docs.google.com", "talkappi.com", "form.run", "formrun.com", "secure-link.jp", "formzu.net", "kintoneapp.com")

FILE = Path(__file__).resolve().parent / "出力" / "NGリスト.txt"
HP_COL = "HP URL"
FORM_COL = "問い合わせフォームURL 無ければメール"


def domain(url):
    u = unicodedata.normalize("NFKC", (url or "").split("#")[0]).strip()
    if not u:
        return ""
    u = u if re.match(r"(?i)https?://", u) else "http://" + u.lstrip("/")
    return (urlparse(u).hostname or "").rstrip(".").removeprefix("www.")


def _shared(h):
    return any(h == s or h.endswith("." + s) for s in SHARED + PATH_HOSTED)


def norm_url(url):
    """http/https・www・末尾スラッシュ・#以降の違いを無視して同じフォームを同じ文字列にする。
    ?以降も無視する(?from=top のような計測用)。ただし複数の会社が使う共有サービスでは、?以降が会社の見分けなので残す"""
    u = unicodedata.normalize("NFKC", (url or "").split("#")[0]).strip()
    if not u:
        return ""
    p = urlparse(u if re.match(r"(?i)https?://", u) else "http://" + u.lstrip("/"))
    h = (p.hostname or "").rstrip(".").removeprefix("www.")
    return h + p.path.rstrip("/").lower() + (("?" + p.query) if p.query and _shared(h) else "")


def load():
    """(ドメインの集合, フォームURLの集合)"""
    doms, urls = set(), set()
    if FILE.exists():
        for line in FILE.read_text(encoding="utf-8-sig").splitlines():
            item = line.split("#")[0].strip()
            if item.startswith("@"):
                urls.add(norm_url(item[1:]))
            elif item:
                doms.add(domain(item.lower()) or item.lower())
    return doms, urls


def _dom_hit(url, doms):
    h = domain(url)
    return bool(h) and any(h == d or h.endswith("." + d) for d in doms)


def is_ng(url, ng=None):
    """ドメイン単位のNGに当たるか(会社のHPで判定する)"""
    return _dom_hit(url, (ng or load())[0])


def is_ng_form(form_url, ng=None):
    return bool(form_url) and norm_url(form_url) in (ng or load())[1]


def is_ng_row(row, ng=None):
    """一覧の1行がNGか。HPのドメイン・フォームURLのどちらかが当たれば"""
    ng = ng or load()
    hp, form = row.get(HP_COL) or "", row.get(FORM_COL) or ""  # 列が足りないCSVでも落ちない
    return is_ng(hp, ng) or is_ng_form(form, ng) or is_ng_form(hp, ng)


def _clean(s):
    """外部由来の会社名・理由に含まれる改行・制御文字を空白にする(1件のNGが別の行を作らないように)"""
    return re.sub(r"[\x00-\x1f\x7f\u2028\u2029]+", " ", s or "")


def _write(line):
    """追記はロックの中で。ファイルが新規のときの文字コードの印(BOM)も、競合せず1回だけ付く"""
    FILE.parent.mkdir(exist_ok=True)
    with filelock.locked(FILE):
        with open(FILE, "a", encoding="utf-8-sig") as f:
            f.write(_clean(line).rstrip() + "\n")


def remove_line(line):
    """NGリストから、その1行(前後の空白を除いて同じもの)だけを外す。読み直しから書き戻しまでをロックの中で行い、
    その間に別の実行が追記した行を消さない。外せたら True"""
    if not FILE.exists():
        return False
    with filelock.locked(FILE):
        lines = FILE.read_text(encoding="utf-8-sig").splitlines()
        hit = next((i for i, l in enumerate(lines) if l.strip() == line.strip()), None)
        if hit is None:
            return False
        del lines[hit]
        filelock.atomic_write(FILE, "\n".join(lines) + ("\n" if lines else ""))
        return True


TOO_WIDE = {"jp", "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "lg.jp", "com", "net", "org"}  # 広すぎる。入れると無関係の会社が全部消える


def add(url, reason, name=""):
    """追加できたら True。入っている・広すぎる・ドメインなしは False。
    共有サービス(パスで店を分けるものを含む)は、ドメインではなくそのURL1つだけを入れる"""
    h = domain(url)
    if not h or "." not in h or h in TOO_WIDE:
        return False
    if _shared(h):
        u = (url or "").strip()
        return add_form(u if re.match(r"(?i)https?://", u) else "https://" + u.lstrip("/"), reason, name)
    if is_ng(h):
        return False
    _write(f"{h}  # {reason} {name}")
    return True


def add_form(form_url, reason, name=""):
    """フォームURL1つだけを追加(自動で送れないフォーム用)"""
    if not form_url.startswith("http") or is_ng_form(form_url):
        return False
    _write(f"@{form_url.strip()}  # {reason} {name}")
    return True
