"""提案書の専用リンク(店舗ごと)と、開かれたかの記録。実行環境: Windows / Python 3.9+, 標準ライブラリのみ。

仕組み
  ・sender.json の「資料URL」に https://<自社サイト>/r/?t={識別} の形で書くと、文面の {資料URL} が会社ごとの専用リンクになる。
    識別子は「乱数8桁+署名8桁」の16桁。署名は計測用の鍵(秘書の認証情報の置き場の formlist_計測キー.txt か、環境変数 FORMLIST_TRACK_KEY)で作る。
    サーバーは署名の合う識別子だけを記録するので、外から識別子を作って記録を荒らしたり、偽の開封を作ったりできない。
    識別子は会社ごとに1回だけ作り、出力/資料リンク.csv に残す(プレビューと本番で同じリンクになる)。
  ・サーバー側(自社サイトの /r/)が、リンクに触れた回数と「開いた合図(JavaScriptが動いた)」を数える。ここでは識別子を渡して集計を取りに行く。
数え方の意味
  ・開いた合図あり(JavaScriptが動いた) = 中継ページのJavaScriptが動いた合図が届いた。人がブラウザで開くと出る。ただし
    JavaScriptを動かす自動確認(リンクの安全確認サービスの検査環境など)も出しうるので「人が必ず開いた」の証明ではない。
  ・触れただけ = リンクにアクセスがあったが合図は無い。メールの自動確認・リンクの下見・JavaScriptを止めた閲覧が含まれる。
  ・送信直後 = 送信ログの時刻(分単位なので最大59秒の誤差を足す)から3分以内の合図だけ。受信側の自動確認の可能性として分ける。
  ・送り直した会社は、合図の直前の送信を基準に判定する。最初の送信より前の合図(運用者の確認)は数えない。
原理的に取りこぼす型: 開いたが提案書を読んだかは分からない / 転送された先の人も同じ識別子で数えられる(識別子を知った人は
  自分の開封記録も読める) / 同じ会社名の別支店は同じリンクを使う / 運用者が送信後に自分でリンクを試すと開封に数えられる /
  サーバー全体のアクセスログ(自前の記録とは別)には、IPアドレスなどが残りうる。
"""
import csv
import datetime as dt
import hmac
import hashlib
import json
import os
import re
import secrets
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import filelock

HERE = Path(__file__).resolve().parent
LINKS = HERE / "出力" / "資料リンク.csv"
SEND_LOG = HERE / "出力" / "送信ログ.csv"
KEY_FILE = HERE / "計測キー.txt"  # 保存先。更新(install.ps1)でも消さない。設定の文の「計測キー」から入る
LEGACY_KEY_FILE = Path.home() / ".claude" / "secretary" / "formlist_計測キー.txt"  # 運用者の古い置き場(読むだけ)
KEY_OK = re.compile(r"[0-9A-Za-z_-]{32,128}")
COLS = ["企業名", "識別子", "作成日時"]
MARK = "{識別}"
IMMEDIATE_SEC = 180
LOG_MINUTE_SLACK = 59  # 送信ログは分単位で切り捨てて書かれるため、実際の送信は最大59秒後
TEMPLATE_OK = re.compile(r"https://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s{}?#@]*)?/\?t=\{識別\}")  # 暗号化なし・ユーザー情報(@)・#を持つ形は通さない
# 「送信試行」は押す直前に書く行で、押さずに終わる経路がある。「手で送信済み」「届いた(本人確認)」の時刻は運用者が記録した時刻(実際の送信より遅れうる)。
SENT_RESULTS = ("送信済み", "不明(要確認)", "手で送信済み", "届いた(本人確認)")
S_UNSENT, S_UNOPENED, S_TOUCH = "未送信", "未開封", "触れただけ(自動確認の可能性)"
S_IMMEDIATE, S_OPENED = "開いた(送信直後・自動確認の可能性)", "開いた合図あり(JavaScriptが動いた)"
_ABBREV = (("(株)", "株式会社"), ("(有)", "有限会社"), ("(合)", "合同会社"), ("(同)", "合同会社"))


def _key(name):
    s = "".join(unicodedata.normalize("NFKC", str(name)).split())
    if s[:1] == "'" and s[1:2] in ("=", "+", "-", "@"):
        s = s[1:]  # _cell が付けた数式除けの ' は、突き合わせでは無いものとして扱う(送信ログには付かない)
    for a, b in _ABBREV:
        s = s.replace(a, b)
    return s


def _cell(name):
    """CSVの先頭が = + - @ のとき、表計算ソフトで数式として動かないよう ' を付ける(会社名は外部サイト由来)。"""
    s = str(name)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _read(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _secret():
    k = os.environ.get("FORMLIST_TRACK_KEY", "").strip()
    for f in (KEY_FILE, LEGACY_KEY_FILE):
        if not k:
            try:
                k = f.read_text(encoding="utf-8").strip()
            except OSError:
                k = ""
    if not KEY_OK.fullmatch(k):
        raise RuntimeError("計測用の鍵がまだ入っていません。担当者から届いた設定の文を、Claude Code に貼って送ると入ります。鍵が無いと、開封を数えられない専用リンクになるため、資料URLは保存しません")
    return k.encode("utf-8")


def save_key(key):
    """計測キーを保存する(sender.json には書かない・応答にも出さない)。形が違えば ValueError。"""
    key = str(key).strip()
    if not KEY_OK.fullmatch(key):
        raise ValueError("計測キーは、英数字と - _ だけの32〜128文字です。設定の文のとおりに貼ってください")
    KEY_FILE.write_text(key, encoding="utf-8")


def _mint(secret):
    r = secrets.token_hex(4)
    return r + hmac.new(secret, r.encode("ascii"), hashlib.sha256).hexdigest()[:8]


def check_template(template):
    """{識別} を含む資料URLの形を確かめる(集計ページを同じ階層に置くため、/…/?t={識別} の形だけ)。"""
    if MARK in template and not TEMPLATE_OK.fullmatch(template):
        raise ValueError("資料URLの {識別} は https://ホスト/階層/?t={識別} の形で、1回だけ書いてください")


def token_for(name, path=None, secret=None):
    """会社ごとの識別子。無ければ作って残す。全角半角・空白・(株)と株式会社の違いは同じ会社として扱う。"""
    path = Path(path or LINKS)
    k = _key(name)
    if not _key(name):
        raise ValueError("会社名が空のため、専用リンクを作れません")
    secret = secret or _secret()
    path.parent.mkdir(parents=True, exist_ok=True)
    with filelock.locked(path):
        for r in _read(path):
            if _key(r.get("企業名", "")) == k and r.get("識別子"):
                return r["識別子"]
        tok = _mint(secret)
        new = not path.exists()
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(COLS)
            w.writerow([_cell(name), tok, dt.datetime.now().strftime("%Y-%m-%d %H:%M")])
        return tok


def url_for(template, name, path=None, secret=None):
    """資料URLの {識別} を会社ごとの識別子に置き換える。{識別} が無ければ全社共通のURLのまま(開封は数えない)。"""
    if MARK not in template:
        return template
    check_template(template)
    return template.replace(MARK, token_for(name, path, secret))


def endpoint(template):
    """資料URLの雛形から、集計を返すページのアドレスを作る(同じ階層の log.php)。"""
    check_template(template)
    p = urllib.parse.urlsplit(template.replace(MARK, "0" * 16))
    return f"{p.scheme}://{p.netloc}{p.path}log.php?t="


def fetch(template, tokens, opener=None):
    """識別子の集計を取る。{識別子: {confirmed, bot_confirmed, touch, bot_touch, first, last, confirmed_ts, confirmed_last, touch_ts}}。通信できなければ止める。"""
    opener = opener or (lambda u: urllib.request.urlopen(u, timeout=20))
    base = endpoint(template)
    out = {}
    toks = list(tokens)
    for i in range(0, len(toks), 100):
        chunk = toks[i:i + 100]
        try:
            with opener(base + ",".join(chunk)) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"開封の記録を取りに行けませんでした({type(e).__name__}: {e})。通信を確かめて、もう一度呼んでください") from e
        if not isinstance(d, dict):
            raise RuntimeError("開封の記録の形が想定と違います(集計ページが別の内容を返しました)")
        meta = d.pop("_meta", None)
        if isinstance(meta, dict) and meta.get("writable") is False:
            raise RuntimeError("サーバーの記録フォルダに書き込めない状態です。開封が記録されていない可能性があるため、0件とは扱いません")
        missing = [x for x in chunk if x not in d]
        if missing:
            raise RuntimeError(f"サーバーが {len(missing)} 件の識別子を受け付けませんでした(署名が合わない=PC側の鍵とサーバーの鍵がずれている可能性)。0件とは扱いません")
        out.update(d)
    return out


def _sent_times(send_log):
    """会社ごとの、押した時刻の一覧(古い順)。送信ログに結果がある行だけ。送り直した会社は複数入る。"""
    out = {}
    for r in _read(send_log):
        if r.get("結果") in SENT_RESULTS:
            try:
                out.setdefault(_key(r.get("企業名", "")), []).append(dt.datetime.strptime(r.get("日時", ""), "%Y-%m-%d %H:%M"))
            except ValueError:
                pass
    return {k: sorted(v) for k, v in out.items()}


def _fmt(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def _classify(conf_ts, conf_last, sends):
    """合図の時刻を、直前の送信を基準に判定する。None=受け手の合図は無い(送信前の確認だけ)、'late'=人が開いた可能性が高い、'immediate'=送信直後だけ。"""
    stamps = sorted(set(list(conf_ts) + ([conf_last] if conf_last else [])))
    seen = []
    for t in stamps:
        d = dt.datetime.fromtimestamp(t)
        prior = [s for s in sends if s <= d]
        if not prior:
            continue  # 最初の送信より前=運用者の確認。数えない
        seen.append(d - max(prior) > dt.timedelta(seconds=LOG_MINUTE_SLACK + IMMEDIATE_SEC))
    if not seen:
        return None
    return "late" if any(seen) else "immediate"


def report(template, links_path=None, send_log=None, fetcher=None):
    """会社ごとの開封の状態。専用リンクを作った会社(プレビュー・送信で使った会社)が対象。
    status: 未送信 / 未開封 / 触れただけ(自動確認の可能性) / 開いた(送信直後・自動確認の可能性) / 開いた合図あり(JavaScriptが動いた)"""
    links = _read(Path(links_path or LINKS))
    if not links:
        return {"companies": [], "total": 0, "note": "専用リンクを作った会社がまだありません(sender.json の資料URLに {識別} を入れて、プレビューか送信をしてください)"}
    sent = _sent_times(send_log or SEND_LOG)
    stats = (fetcher or (lambda toks: fetch(template, toks)))([r["識別子"] for r in links if r.get("識別子")])
    rows = []
    for r in links:
        s = stats.get(r["識別子"], {})
        confirmed, touch, bot = int(s.get("confirmed", 0)), int(s.get("touch", 0)), int(s.get("bot_touch", 0))
        sends = sent.get(_key(r["企業名"]), [])
        verdict = _classify(s.get("confirmed_ts", []), s.get("confirmed_last"), sends) if confirmed else None
        if not sends:
            status = S_UNSENT
        elif verdict == "late":
            status = S_OPENED
        elif verdict == "immediate":
            status = S_IMMEDIATE
        elif touch or int(s.get("bot_confirmed", 0)) or confirmed:
            status = S_TOUCH
        else:
            status = S_UNOPENED
        rows.append({"company": r["企業名"], "status": status, "confirmed": confirmed, "touch": touch, "bot_touch": bot,
                     "first": _fmt(s.get("first")), "last": _fmt(s.get("last")),
                     "sent": sends[-1].strftime("%Y-%m-%d %H:%M") if sends else "", "send_count": len(sends)})
    summary = {}
    for x in rows:
        summary[x["status"]] = summary.get(x["status"], 0) + 1
    return {"companies": rows, "total": len(rows), "summary": summary,
            "note": "開いた=ブラウザでJavaScriptが動いた合図。人が必ず開いた証明ではなく、提案書を読んだかも分からない。転送先の人も同じ会社として数える。"
                    " 運用者が送信後に自分でリンクを試した分も数える。開いたが連絡の無い会社に送り直すときは、送信の関門(同じ会社は14日で2回まで)の範囲で、文面を利用者に全文見せてから送る"}
