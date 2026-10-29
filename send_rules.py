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
from pathlib import Path
from urllib.parse import urlparse

import ng_list
import state

ENFORCE_WINDOW = True  # テストが時刻に左右されないよう、conftest が切る
HOURS = (9, 18)
DAILY_LIMIT = 300
REPEAT_DAYS = 14
MAX_PER_COMPANY = 2
PRESSED = ("送信試行", "送信済み", "不明(要確認)", "入力エラー(要確認)", "手で送信済み")  # 押した(か、押したかもしれない)記録
OPT_OUT = re.compile(r"配信停止|配信を停止|今後.{0,15}(不要|お送りしません|送らない|ご遠慮)|(ご案内|メール|連絡|営業).{0,10}(不要|ご遠慮)|不要(でしたら|な場合|の場合|であれば)|不要と(ご)?(返信|ご連絡)")  # 「不要な手間」のような別の文脈の「不要」では通さない
PHONE = re.compile(r"\d{2,4}-\d{2,4}-\d{3,4}")


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
    if not PHONE.search(sig):
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
        return f"きょうは{'土日'[now.weekday() - 5]}曜日です。営業の問い合わせは平日の{HOURS[0]}時〜{HOURS[1]}時に送ります。平日にもう一度「確認する」からやり直してください"
    if not (HOURS[0] <= now.hour < HOURS[1]):
        return f"いまは{now.hour}時台です。営業の問い合わせは平日の{HOURS[0]}時〜{HOURS[1]}時に送ります。その時間にもう一度「確認する」からやり直してください"
    return None


def _rows(log):
    try:
        with open(log, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _when(r):
    try:
        return dt.datetime.strptime((r.get("日時") or "")[:16], "%Y-%m-%d %H:%M")
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
    for r in _rows(log):
        if r.get("結果") not in PRESSED:
            continue
        if company_key(r.get("企業名")) in me or form_domain(r.get("フォームURL")) in doms:
            t = _when(r)
            if t:
                hits.append(t)
    if not hits:
        return None
    sends = len({t.date() for t in hits})
    last = max(hits)
    if sends >= MAX_PER_COMPANY:
        return f"この会社には、すでに{sends}回送っています(最後は{last:%m月%d日})。これ以上は送りません"
    if now - last < dt.timedelta(days=REPEAT_DAYS):
        return f"この会社には{last:%m月%d日}に送っています。{REPEAT_DAYS}日たつまで、同じ会社には送りません"
    return None
