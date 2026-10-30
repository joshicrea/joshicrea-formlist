"""本番送信の決まりごと(法務と送る量)。実行環境: Windows / Python 3.9+
営業の手順書「法務とリスク」の送信ルールを、警告ではなく送信を止める形にしたもの。
  ・法定表示: 署名に、住所・電話・配信停止の案内(「不要」とご返信ください 等)が無ければ送らない
  ・時間帯: 平日の 9時〜18時 以外は送らない(受け取った側の印象を守る)
  ・1日の上限: その日に押した社数を、回をまたいで数える(既定300社。運用者が「1日数百件」と決めた値)
  ・同じ会社: 送ってから14日は、同じ会社(会社名・フォームのドメイン)へ送らない。14日を過ぎても、2回送った会社にはもう送らない
数値の出どころ: 14日と2回は 09 の送信ルール2「同じ企業へは1回だけ。返信が無ければ14日後に別の窓口へ1回まで」。
          9時〜18時は 09 の送信ルール6「平日の日中」をこの時間と読んだもの。1日の上限は 09 に無く、営業方針の値。2026-10-21 に運用者が「1日数百件にアプローチしたい」と決め、20社から300社へ上げた(送信の間隔30〜60秒のまま、9〜18時で最大およそ650社まで送れる)。
守備範囲: 祝日は見ていない(平日の扱いになる)。会社名の表記違い(株式会社の有無以外)と、別ドメインの同じ会社は同じ会社と分からない。
          数えるのは このパソコンの送信ログと送信状態のDBだけ(別のパソコンで送った分は数えない)。
          同時に動く複数の送信は、押す直前に reserve_daily でDB上の枠を1社ずつ確保するので、上限を超えない。
"""
import csv
import datetime as dt
import re
import secrets
import sqlite3
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import ng_list
import placeholders
import state

ENFORCE_WINDOW = True  # テストが時刻に左右されないよう、conftest が切る
HOURS = (9, 18)
DAILY_LIMIT = 300
REPEAT_DAYS = 14
MAX_PER_COMPANY = 2
PRESSED = ("送信試行", "送信済み", "不明(要確認)", "入力エラー(要確認)", "手で送信済み")  # 押した(か、押したかもしれない)記録
OPT_OUT = re.compile(r"配信停止|配信を停止|今後.{0,15}(不要|お送りしません|送らない|ご遠慮)|(ご案内|メール|連絡|営業).{0,10}(不要|ご遠慮)|不要(でしたら|な場合|の場合|であれば)|不要と(ご)?(返信|ご連絡)")  # 「不要な手間」のような別の文脈の「不要」では通さない
PHONE = re.compile(r"\d{2,4}-\d{2,4}-\d{3,4}")  # 以前の判定(互換のため残す。署名の電話の有無は phone_numbers で見る)
PHONE_LIKE = re.compile(r"\+?\d[\d\-‐‑‒–—―−ー()\s]{7,}\d")  # NFKC のあとの1行の中で、数字と区切り(ハイフン類・括弧・空白)だけが続く並び


def phone_numbers(text):
    """文の中の電話番号らしい数字の並び(数字だけにしたもの)の一覧。全角・ハイフンなし・括弧つき・空白区切り・+81 で始まる形を読む。
    10桁か11桁のもの(+81 の形は先頭の81を0に戻して数える)だけを返す。
    陽性対照: 「03-0000-0000」「０３－００００－００００」「0300000000」「03(0000)0000」「03 0000 0000」「+81-3-0000-0000」→ どれも "0300000000"。
    誤爆の対照: 郵便番号「100-0005」(7桁)・日付「2026-10-30」(8桁)・番地「1-1」は返さない。
    原理的に取りこぼす型: 漢数字で書いた番号・行をまたいで書いた番号・12桁以上の並びの中に埋もれた番号(別の数字と区切り無しでつながる形)"""
    def norm(s):
        digits = re.sub(r"\D", "", s)
        if s.startswith("+81") or (s.startswith("81") and len(digits) in (11, 12)):
            digits = "0" + digits[2:]
        return digits if len(digits) in (10, 11) else ""

    out = []
    for line in unicodedata.normalize("NFKC", text or "").splitlines():
        for m in PHONE_LIKE.finditer(line):
            whole = norm(m.group(0).strip())
            if whole:
                out.append(whole)
                continue
            parts = m.group(0).split()  # 「丸の内1-1 03-0000-0000」のように空白で隣の数字とつながった並びは、空白で切った続きの組で探す
            for i in range(len(parts)):
                for j in range(i + 1, len(parts) + 1):
                    got = norm(" ".join(parts[i:j]))
                    if got:
                        out.append(got)
    return out


MAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def signature_mismatch(sender):
    """署名に書いた連絡先(メール・電話)が、送信元の欄の値と食い違う欄の一覧 [(欄の名前, 署名に書いてある値)]。
    署名にその種類の連絡先が1つも無ければ数えない(有無は legal_missing が見る)。署名に複数あれば、どれか1つが欄と同じなら一致とする(電話とFAXなど)。
    比べ方: メールは NFKC・小文字・前後の空白を揃えて完全一致。電話は phone_numbers で数字だけにして完全一致。
    陽性対照: 電話欄 090-1111-2222・署名 03-0000-0000 → [("電話", "03-0000-0000")]。メール欄 other@example.com・署名 hanako@example.com → [("メール", …)]。
    誤爆の対照: 電話欄 03-0000-0000・署名 ０３（００００）００００ → []。署名にメールが無い → []。
    原理的に取りこぼす型: 欄と署名の両方が同じ古い値のまま(どちらも正しくない)。住所の番地違い(住所は legal_missing が欄の値の全体を探す)"""
    sig = sender.get("署名", "") or ""
    out = []
    mail = unicodedata.normalize("NFKC", str(sender.get("メール") or "")).strip().lower()
    in_sig = MAIL_IN_TEXT.findall(unicodedata.normalize("NFKC", sig))
    if mail and in_sig and mail not in {m.lower() for m in in_sig}:
        out.append(("メール", in_sig[0]))
    field = phone_numbers(str(sender.get("電話") or ""))
    sig_phones = phone_numbers(sig)
    if field and sig_phones and field[0] not in sig_phones:
        shown = next((m.group(0).strip() for m in PHONE_LIKE.finditer(unicodedata.normalize("NFKC", sig))), sig_phones[0])
        out.append(("電話", shown))
    return out


def legal_missing(sender):
    """署名に足りない法定表示の一覧。空なら送れる"""
    sig = sender.get("署名", "") or ""
    miss = []
    addr = (sender.get("住所") or "").strip()
    if not addr or re.sub(r"\s", "", addr) not in re.sub(r"\s", "", sig):  # 番地・建物名まで署名に入っていること(先頭だけでは通さない)
        miss.append("住所(送信元の住所を署名に入れてください)")
    names = [n for n in ((sender.get(k) or "").strip() for k in ("会社名", "氏名")) if len(n) >= 2]
    compact_sig = re.sub(r"\s", "", sig)
    if not names or not any(re.sub(r"\s", "", n) in compact_sig for n in names):
        miss.append("送信者の氏名・名称(送信元の会社名か氏名を設定し、署名に入れてください)")
    if not phone_numbers(sig):  # 全角・ハイフンなし・括弧つきも電話番号として読む(以前は 03-0000-0000 の形だけ)
        miss.append("電話番号")
    if not OPT_OUT.search(sig):
        miss.append("配信停止の案内(例: 今後ご案内が不要でしたら、このアドレスに「不要」とご返信ください)")
    return miss


def window_refusal(now=None):
    """送ってよい時間帯でなければ、その理由。送れるなら None"""
    if not ENFORCE_WINDOW:
        return None
    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return f"きょうは{'土日'[now.weekday() - 5]}曜日です。送れるのは平日{HOURS[0]}時〜{HOURS[1]}時です。送る内容を出すところまではできます"
    if not (HOURS[0] <= now.hour < HOURS[1]):
        return f"いまは{now.hour}時台です。送れるのは平日{HOURS[0]}時〜{HOURS[1]}時です。送る内容を出すところまではできます"
    return None


REQUIRED = ("会社名", "氏名", "フリガナ", "メール", "電話", "住所", "件名", "署名")  # 送る前に必ず要る欄(勝利条件 S群の決定)。郵便番号は任意、資料URLは文面が使うときだけ
MAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def sender_problems(sender, templates=()):
    """送信元で、送る前に直す必要があるものの一覧。空なら送れる(文面そのものの見本の文字は、送る内容を作るところで見る)。
    返す形: [{"field": 欄の名前, "kind": "未保存"|"空"|"見本のまま"|"形が違う"|"署名に足りない"|"資料URLが空", "text": 利用者向けの1文}]
    sender: 保存した送信元(dict)。一度も保存していなければ None。templates: 使う文面の本文(文字列)の並び。{資料URL} を含む文面があるときだけ資料URLを必須にする。
    陽性対照: None/各必須欄が空/【屋号】のまま/@の無いメール/署名に電話が無い/文面が資料のリンクを使うのに資料URLが空 → それぞれ欄の名前つきで返る。
    誤爆の対照: 揃った送信元(件名の {企業名}・資料URLの {識別} を含む)と、資料のリンクを使わない文面 → 空。
    原理的に取りこぼす型: 本物らしいが間違った値(古いメール・別人の電話)。電話の形(桁数)は見ない。見本に無い語を括弧なしで書いた仮の値"""
    if sender is None:
        return [{"field": "", "kind": "未保存", "text": "送信元がまだ入っていません。最初の1回だけ入れます"}]
    words = placeholders.sample_words()
    out = []
    for k in REQUIRED:
        if not str(sender.get(k) or "").strip():
            out.append({"field": k, "kind": "空", "text": f"送信元の「{k}」が空です"})
    for k, v in sender.items():
        v = str(v or "")
        if not v.strip():
            continue
        checked = v.replace("{企業名}", "") if k == "件名" else v.replace("{識別}", "") if k == "資料URL" else v
        left = placeholders.leftovers(checked, words, allow_heading=False)
        if left:
            w = left[0]
            what = f"見本の「{w['word']}」のまま" if w["kind"] == "見本" else f"「{w['match']}」の部分が置き換わらない仮の文字のまま"
            out.append({"field": k, "kind": "見本のまま", "text": f"送信元の「{k}」が{what}です"})
    mail = str(sender.get("メール") or "").strip()
    if mail and not any(o["field"] == "メール" for o in out) and not MAIL.fullmatch(mail):
        out.append({"field": "メール", "kind": "形が違う", "text": "送信元の「メール」がメールアドレスの形になっていません" + ("(「@」がありません)" if "@" not in mail else "")})
    if not any(o["field"] == "署名" for o in out):
        for m in legal_missing(sender):
            label = m.split("(")[0]
            out.append({"field": "署名", "kind": "署名に足りない", "text": f"署名に「{label}」が入っていません。法律で決まっている表示です(住所・電話・お名前・不要ならご返信をの案内)"})
        for k, in_sig in signature_mismatch(sender):  # 欄を直して署名を直し忘れると、古い連絡先の署名のまま全社へ届くため止める
            out.append({"field": "署名", "kind": "署名と欄が違う",
                        "text": f"署名の{k}({in_sig})が、送信元の「{k}」({str(sender.get(k) or '').strip()})と違います。署名か「{k}」の欄を、いまの連絡先にそろえてください"})
    url = str(sender.get("資料URL") or "").strip()
    if any("{資料URL}" in (t or "") for t in templates or ()):
        if not url:
            out.append({"field": "資料URL", "kind": "資料URLが空", "text": "文面に資料のリンクがありますが、送信元の「資料URL」が空です"})
    if url and not any(o["field"] == "資料URL" for o in out) and not re.match(r"https?://\S+$", url):
        out.append({"field": "資料URL", "kind": "形が違う", "text": "送信元の「資料URL」が、https:// で始まるアドレスの形になっていません"})
    return out


def _rows(log):
    try:
        with open(log, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


WHEN = re.compile(r"^\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})日?(?:[T\s]+(\d{1,2})\s*[:時]\s*(\d{1,2}))?")


def _when(r):
    """送信ログの「日時」を読む。ツールが書く「2026-10-27 10:00」に加え、Excel で開いて保存し直した形
    (「2026/10/27 10:00」「2026/10/27 10:00:00」「2026/10/27」・月日の0なし・全角数字)と「2026年10月27日 10時00」を読む。読めなければ None。
    陽性対照: 上の各形 → 同じ日時(時刻が無ければ0時)。誤爆の対照: 空・「不明」→ None。
    原理的に取りこぼす型: 年の無い形(「10/27 10:00」)・月日の順が逆の形(「10/27/2026」)・Excel の通し番号(「46322.4」)。これらは None になり、
    company_refusal は送らない側に倒して数える"""
    m = WHEN.match(unicodedata.normalize("NFKC", r.get("日時") or ""))
    if not m:
        return None
    y, mo, d, h, mi = (int(x) if x else 0 for x in m.groups())
    try:
        return dt.datetime(y, mo, d, h, mi)
    except ValueError:
        return None


def _pressed_pairs(log, now=None):
    today = (now or dt.datetime.now()).date()
    return {(r.get("企業名"), r.get("フォームURL")) for r in _rows(log)
            if r.get("結果") in PRESSED and (_when(r) or dt.datetime.min).date() == today}


def pressed_today(log, now=None):
    """きょう押した社数(同じ会社・フォームの重複は1社)"""
    return len(_pressed_pairs(log, now))


def _quota_conn(db):
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db), timeout=30, isolation_level=None)
    c.execute("CREATE TABLE IF NOT EXISTS daily_slots (token TEXT PRIMARY KEY, day TEXT, company TEXT, url TEXT, pressed INTEGER, created REAL)")
    return c


def reserve_daily(db, log, name, url):
    """押す直前に、1日の上限の枠を1社ぶん確保する。確保できたら印(文字列)、上限なら None。
    同時に動く複数の送信が、それぞれ「きょうの数<上限」を見てから押して上限を超えるのを、DBの BEGIN IMMEDIATE で1本ずつにして防ぐ。
    数えるのは 送信ログのきょう押した会社 と DBの押した枠・確保中の枠 の和(同じ会社・フォームは1社。ログとDBの両方にあっても二重に数えない)。
    押さずに止まったまま settle_daily が呼ばれなかった確保(強制終了など)は、state.RECOVER_AFTER 秒で数えなくなる"""
    today = dt.date.today().isoformat()
    c = _quota_conn(db)
    try:
        c.execute("BEGIN IMMEDIATE")
        held = c.execute("SELECT company, url FROM daily_slots WHERE day=? AND (pressed=1 OR created>?)",
                         (today, time.time() - state.RECOVER_AFTER)).fetchall()
        used = {tuple(r) for r in held} | _pressed_pairs(log) | {(name, url)}
        if len(used) > DAILY_LIMIT:
            c.execute("ROLLBACK")
            return None
        token = secrets.token_hex(8)
        c.execute("INSERT INTO daily_slots VALUES (?, ?, ?, ?, 0, ?)", (token, today, name, url, time.time()))
        c.execute("COMMIT")
        return token
    except sqlite3.Error:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        c.close()


def settle_daily(db, token, pressed):
    """確保した枠の始末。押した(押したかもしれない)なら数え続け、押さずに終わったなら戻す"""
    c = _quota_conn(db)
    try:
        if pressed:
            c.execute("UPDATE daily_slots SET pressed=1 WHERE token=?", (token,))
        else:
            c.execute("DELETE FROM daily_slots WHERE token=?", (token,))
    finally:
        c.close()


def company_key(name):
    return re.sub(r"\s|株式会社|有限会社|合同会社|\(株\)|（株）", "", name or "")


def form_domain(url):
    h = ng_list.domain(url or "")
    return "" if not h or ng_list._shared(h) else h  # 共有サービス(Googleフォーム等)のドメインは別会社と同じになるので使わない


def company_refusal(log, name, url, hp="", now=None):
    """同じ会社へ送りすぎないための理由。送れるなら None"""
    now = now or dt.datetime.now()
    me = {company_key(name)} - {""}
    doms = {form_domain(url), form_domain(hp)} - {""}
    hits = []
    unreadable = False
    for r in _rows(log):
        if r.get("結果") not in PRESSED:
            continue
        if company_key(r.get("企業名")) in me or form_domain(r.get("フォームURL")) in doms:
            t = _when(r)
            if t:
                hits.append(t)
            else:
                unreadable = True
    if unreadable:  # 押した記録があるのに日付が読めない。間隔を確かめられないので、送らない側に倒す
        return "この会社に送った記録がありますが、送信の記録の日付が読めないため、間隔を確かめられません。この会社には送りません(送信の記録の「日時」を 2026-10-27 10:00 の形に直すと確かめられます)"
    if not hits:
        return None
    sends = len({t.date() for t in hits})
    last = max(hits)
    if sends >= MAX_PER_COMPANY:
        return f"この会社には、すでに{sends}回送っています(最後は{last:%m月%d日})。これ以上は送りません"
    if now - last < dt.timedelta(days=REPEAT_DAYS):
        return f"この会社には{last:%m月%d日}に送っています。{REPEAT_DAYS}日たつまで、同じ会社には送りません"
    return None
