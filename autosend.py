"""フォームリスト作成ツールが作ったCSVの「フォームあり」の会社へ、問い合わせフォームに自動で入力して送る。
実行環境: Windows / Python 3.9+ / playwright（py -m playwright install chromium）

標準は試走: 入力して画面を保存するだけで、送信ボタンは押さない。ただし試走でも実際のサイトの入力欄に入力するため、
サイトによっては入力の途中で内容が先方に送られる作り(入力中の自動保存・チェックで送信するUI等)があり得る。
「何も届かない」は保証できない。本番は --send を付け、
文面の全文と件数を見て「送信」と入力したときだけ送る。
使い方:
  py autosend.py 出力\\フォームリスト_xxx.csv --sender sender.json --message 文面.txt            (試走)
  py autosend.py 出力\\フォームリスト_xxx.csv --sender sender.json --message 文面.txt --send [--max 20]
sender.json: {"会社名":"","氏名":"","フリガナ":"","メール":"","電話":"","郵便番号":"","住所":"","件名":""}
文面.txt: {企業名} が先方の会社名に置き換わる。特電法のため、送信者の名前・連絡先・配信停止の案内を本文に入れる。

安全装置: 営業お断り表示の会社は送らない / NGリスト(formlist.pyの--ng)で除外済みの会社は元のCSVに出ない /
送信済みは送信ログ.csvに残り、2回送らない / captcha・必須の未知項目・選択式の必須があれば送らず「要手動」/
1回の上限 --max(既定20) / 送信の間隔は30〜60秒。
対照(導入時に実測): 試走で入力欄が埋まり送信ボタンを押さないこと / captchaのあるフォームで「要手動」になること。
原理的に取りこぼす型: 項目名が画像のフォーム・複数ステップの独自UI・JSで描画が遅いフォームは「要手動」か「不明」になる。
「送信済み」は完了画面の文言を確認できたときだけ。確認できなければ「不明(要確認)」で、送れたかは先方次第。"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse


import message_adoption
import placeholders
import proposal_links
import send_rules

# Explicit message-only CLI bypasses transport/browser/state imports.
# Normal module consumers and ordinary CLI retain the existing runtime imports.
_PURE_MESSAGE_CHECK = __name__ == "__main__" and any(
    arg == "--check-message-name" or arg.startswith("--check-message-name=")
    for arg in sys.argv[1:]
)
if not _PURE_MESSAGE_CHECK:
    from playwright.sync_api import sync_playwright
    import filelock
    import netguard
    import site_checker
    import state
    import ng_list
    import formlist
    from ng_state_store import NgStateStore
    import sales_records

HERE = Path(__file__).resolve().parent
LOG = HERE / "出力" / "送信ログ.csv"
SHOTS = HERE / "出力" / "送信画面"
sys.path.insert(0, str(HERE))

LOG_COLS = ["日時", "企業名", "フォームURL", "結果", "メモ"]

FIELD_MAP = [  # (正規表現, sender.jsonのキー)。上から順に最初に当たったもの
    (re.compile(r"確認|confirm|re-?mail|再入力", re.I), "メール"),
    (re.compile(r"メール|e-?mail|mail", re.I), "メール"),
    (re.compile(r"フリガナ|ふりがな|カナ|kana", re.I), "フリガナ"),
    (re.compile(r"会社|企業|法人|団体|company|corp|organization", re.I), "会社名"),
    (re.compile(r"件名|題名|subject|title", re.I), "件名"),
    (re.compile(r"電話|tel|phone", re.I), "電話"),
    (re.compile(r"郵便|zip|postal", re.I), "郵便番号"),
    (re.compile(r"住所|address", re.I), "住所"),
    (re.compile(r"氏名|お名前|担当者名|name", re.I), "氏名"),
]
CAPTCHA = re.compile(r"recaptcha|hcaptcha|turnstile|captcha", re.I)
DONE = re.compile(r"送信(が)?完了|送信しました|受け付けました|受付けました|お問い?合わせ(を)?(いただき|頂き)ありがとうございました|送信ありがとうございました|ご登録ありがとうございます|ご案内をお送りしました|has been sent|successfully", re.I)
CONSENT = re.compile(r"同意|承諾|プライバシー|個人情報|privacy|agree", re.I)


def pick_message_source(path, industry):
    """(どの文面か, 本文)。どの文面か は「標準」か、業種ごとの文面の業種名(ファイルを1つ指定したときは「指定の文面」)。
    --message がフォルダなら 業種名.txt を優先し、無ければ割り当てた業種別パターン、それも無ければ default.txt"""
    p = Path(path)
    if p.is_dir():
        # 業種は、書かれたとおりの名前 → 新しい分類の名前(古い名前で作ったリストも、新しい分類の文面を使う) の順に探す
        import industries
        for name in dict.fromkeys([industry, industries.canonical(industry) if industry else industry]):
            f = p / f"{name}.txt"
            # CSVの業種は外部由来。「../」や絶対パスで文面フォルダの外を読ませない(区切り文字を含むか、解決後に直下でなければ default.txt)
            safe = name and not re.search(r'[\\/:*?"<>|]|\.\.', name) and f.resolve().parent == p.resolve()
            if safe and f.exists():
                return name, f.read_text(encoding="utf-8-sig")
        import patterns  # 専用の業種文面が無ければ、割り当てた業種別パターン。割り当てが無い業種は標準へ(人が送信前に確認する)
        for name in dict.fromkeys([industry, industries.canonical(industry) if industry else industry]):
            text = patterns.text_for_industry(name) if name else None
            if text:
                return name, text
        return "標準", (p / "default.txt").read_text(encoding="utf-8-sig")
    return "指定の文面", p.read_text(encoding="utf-8-sig")


def pick_message(path, industry):
    """--message がフォルダなら 業種名.txt を優先し、無ければ default.txt。ファイルならそれを使う"""
    return pick_message_source(path, industry)[1]


class EmptyOpenerError(ValueError):
    """書き出し差込がある会社の空欄。参照の有無までは判定しない。"""


def render(text, name, opener, sender):
    """{企業名}{書き出し}{署名}を置き換える。書き出し差込がある文面は、空欄なら停止する"""
    if "{書き出し}" in text and not opener.strip():
        raise EmptyOpenerError("書き出しが空欄のため、この会社の文面生成を停止しました")
    text = text.replace("{企業名}", name).replace("{署名}", sender.get("署名", "")).replace("{書き出し}", opener.strip())
    if sender.get("資料URL"):  # 空のときは置き換えない: {資料URL}が残り、送信前の「差し込みの取り残し」で止まる
        # 資料URLに {識別} があれば、会社ごとの専用リンクにする(開かれたかを会社単位で数えるため)
        text = text.replace("{資料URL}", proposal_links.url_for(sender["資料URL"], name))
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def prepare_message_plan(todo, message_path, sender):
    """会社単位の空書出しを除外し、対象と本文の対応を保つ。IO等の別障害は握りつぶさない。"""
    ready, bodies, rejected = [], [], []
    for row in todo:
        try:
            row, template, local_sender = message_adoption.resolve_row(row, message_path, sender, pick_message)
            name, url, industry, opener, _hp = row
            body = render(template, name, opener, local_sender)
            message_adoption.validate_body(name, url, body, sender)
        except (EmptyOpenerError, message_adoption.AdoptionError) as exc:
            name, url = row[:2]
            rejected.append((name, url, str(exc)))
            continue
        ready.append(row)
        bodies.append(body)
    return ready, bodies, rejected



def plan_digest(todo, bodies, sender):
    """送る会社・各社の全文・送信元(フォームに入れる値を全部含む)の照合番号。確認(--preview)と本番(--send --expect)で同じ関数を使う"""
    rows = [(t[0], t[1], b, message_adoption.effective_sender(t[0], t[1], sender)) for t, b in zip(todo, bodies)]
    return hashlib.sha256(json.dumps({"rows": rows, "sender": sender, "adoptions": message_adoption.record_digest()}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


CONTACT = re.compile(r"@|\d{2,4}-\d{2,4}-\d{3,4}")  # 最終の本文に送信者の連絡先(メールか電話)があるか


def subject_for(name, url, sender):
    """その会社に実際に入れる件名(採用記録の送信元を反映し、{企業名}を置き換えたもの)"""
    return message_adoption.effective_sender(name, url, sender).get("件名", "").replace("{企業名}", name)


MASK = "〇"  # 仮の文字の検査で、相手の会社名・書き出しの部分の代わりに置く1文字(括弧ではないので、それ自体は止まらない)


def _masked(text, *parts):
    """text の中の parts(相手の会社名・書き出し)を MASK に置き換える。長いものから置き換える(書き出しの中の会社名を先に崩さない)"""
    for p in sorted({(p or "").strip() for p in parts} - {""}, key=len, reverse=True):
        text = text.replace(p, MASK)
    return text


def body_leftovers(body, name, opener, words=None, headings=None):
    """全文に残った仮の文字。ただし相手の会社名と書き出し(ホームページの題から作る)の部分は見ない。
    「【公式】ホテル…」のような題・会社名は相手の側の文字で、文面の仮の文字ではないため(勝利条件 D7: 事実と違う理由で外さない)。
    陽性対照: 書き出しに【公式】があり、文面にも【拠点エリア】が残る → 【拠点エリア】で止まる。
    誤爆の対照: 書き出し「ホームページの「【公式】ホテルhotel」を拝見しました」・会社名「【公式】hotel」だけ → 止まらない。
    原理的に取りこぼす型: 会社名か書き出しの文字列と一字一句同じ仮の文字が文面にある場合(例: 会社名が「【屋号】」の行)。
    その部分も会社名として見ないため。会社名を括弧で囲む文面「【{企業名}】」は MASK を囲む【〇】として止まる"""
    return placeholders.leftovers(_masked(body, name, opener), words, headings=headings)


def subject_leftovers(name, url, sender, words=None):
    """件名に残った仮の文字。{企業名} に入る相手の会社名の部分は見ない(会社名の【公式】で「件名が置き換わらない」と言わない)"""
    template = message_adoption.effective_sender(name, url, sender).get("件名", "")
    return placeholders.leftovers(template.replace("{企業名}", MASK), words, allow_heading=False)


SKIP_TEXT = (  # target_skip_reasons の理由 → (reason_code, 利用者向けの文)。上から先に当たったもの
    ("送信済み", "sent", "このフォームには、すでに送ったか、リストの別の会社と同じフォームです(同じフォームへは1回だけ送ります)"),
    ("営業お断り", "refuse", "ホームページに営業お断りの表示があるため、送りません"),
    ("NGリスト", "ng", "送らない会社に入っているため、送りません"),
    ("検索補完", "hp_supplemented", "ホームページを検索で補った会社のため、自動では送りません(別の会社のページの恐れがあるため)"),
    ("HPの照合", "hp_verify", "ホームページがこの会社のものか(電話番号か住所の一致で)確かめきれていないため、自動では送りません"),
    ("http", "form_url", "問い合わせフォームのアドレスが正しい形ではないため、送りません"),
)


def _skip_item(why):
    """target_skip_reasons の理由を、画面の skipped の形にする。フォームなしの行(フォーム判定がフォームありではない)は、送る対象の候補ではないので None。
    送らない会社(NGリスト)も None: 利用者が自分で「送らない」に入れた会社で、結果の一覧からも外れる。既存の試験
    (test_app_offline.test_api_targets_and_send_preview_use_official_research_output_with_isolated_state の skipped == [])がこの扱いを固定している"""
    if why.startswith("フォーム判定") or "NGリスト" in why:
        return None
    for head, code, text in SKIP_TEXT:
        if head in why:
            return {"reason_code": code, "text": text, "fix": "none"}
    return {"reason_code": "other", "text": "自動では送らない会社です: " + why[:60], "fix": "none"}


def build_plan(csv_path, include_supplemented, gate, max_count, message_path, sender, apply_rules=True, explain=False, apply_daily=True):
    """送る会社と各社の全文を決める、ただ1つの関数。確認(画面の「送る内容を出す」・--preview)と本番(--send)は、必ずこれを通す。
    順序: 送る候補(targets: 送らない会社・送信済み・重複を外す) → 同じ会社への間隔(company_refusal) → きょうの残り(1日の上限) →
          max_count で切る → 文面を作る(prepare_message_plan) → 件名と本文の検査(placeholders.leftovers・連絡先) → 確認番号。
    apply_rules=False は試走だけ(間隔ときょうの残りを掛けない。以前の main と同じ)。explain=True のときだけ、送信済み・重複で外した会社も skipped に入れる(画面の表示用。確認番号は変わらない)。
    返す形: {"todo": [(会社名, フォームURL, 業種, 書き出し, HP)], "bodies": [全文], "subjects": [件名], "labels": [どの文面か],
             (labels は explain=True のときだけ埋まる)
             "skipped": [{"name", "url", "reason_code", "text", "fix": "sender"|"message"|"none"}], "digest": 確認番号(送る会社が0なら None),
             "left_today": きょうの残り(apply_rules=False なら None), "over_limit": きょうの残りと max_count で外れた社数, "stop": 1社も送れない理由(無ければ "")}
    reason_code: "sent"(送信済み・重複) "company"(同じ会社の間隔) "opener"(書き出しが空) "adoption"(採用記録との不一致) "subject"(件名の仮の文字) "message"(文面の仮の文字) "sender_url"(資料URLが空) "contact"(本文に連絡先が無い)
    陽性対照(test_send_flow_offline): 14日以内に送った会社・2回送った会社・送信済み・見本の語の残った文面・件名の記号 → skipped に理由つきで出て todo に無い。
    誤爆の対照: 見出し1行の【企業・団体向け提案書】だけの文面は todo に入る。
    原理的に取りこぼす型: 送信ログに無い送信(別のパソコン・手で送って記録しなかった分)。会社名の大きな表記違い(send_rules の守備範囲と同じ)
    件名・全文の仮の文字の検査は、相手の会社名と書き出しの部分を見ない(subject_leftovers・body_leftovers。【公式】の題で文面のせいにしない)。
    explain=True の skipped には、お断り・HPの照合が弱い・検索で補ったHP・フォームのアドレスの形で外した会社も理由つきで入る(フォームなしの行と送らない会社(NG)は入れない)。
    同じ会社(会社名の株式会社の有無・フォームかHPのドメインが同じ)がリストに2行以上あれば、2行目以降を "company" で外す(本番の1社ごとの見直しと同じ数え方)。
    apply_daily=False は、きょうの残りで切らない(確認番号の不一致の理由を調べるときだけ使う)"""
    skipped = []
    if explain:
        for n, u, why in target_skip_reasons(csv_path, include_supplemented, gate=gate):
            item = _skip_item(why)
            if item:
                skipped.append(dict(item, name=n, url=u))
    todo = targets(csv_path, include_supplemented, gate=gate) if gate is not None else targets(csv_path, include_supplemented)
    left = None
    over = 0
    if apply_rules:  # 同じ会社へ送りすぎない(09 送信ルール2)。確認と本番で同じ絞り方にする(確認番号を合わせるため)
        kept, seen_names, seen_doms = [], set(), set()
        for row in todo:
            why = send_rules.company_refusal(LOG, row[0], row[1], row[4])
            key = send_rules.company_key(row[0])
            doms = {send_rules.form_domain(row[1]), send_rules.form_domain(row[4])} - {""}
            if not why and ((key and key in seen_names) or doms & seen_doms):  # 本番では1社目を送ったあと、2社目は「同じ会社」で飛ばされる。確認でも同じに数える
                why = "リストの中に同じ会社がもう1行あります(会社名かホームページが同じ)。同じ会社へは1回だけ送るので、先の行だけに送ります"
            if why:
                skipped.append({"name": row[0], "url": row[1], "reason_code": "company", "text": why, "fix": "none"})
            else:
                kept.append(row)
                seen_names |= {key} - {""}
                seen_doms |= doms
        todo = kept
        if apply_daily:
            left = send_rules.DAILY_LIMIT - send_rules.pressed_today(LOG)
            if left <= 0:
                return {"todo": [], "bodies": [], "subjects": [], "labels": [], "skipped": skipped, "digest": None, "left_today": 0, "over_limit": len(todo),
                        "stop": f"きょうはもう{send_rules.DAILY_LIMIT}社に送りました(1日{send_rules.DAILY_LIMIT}社まで)。あした以降に送ります"}
            over = max(0, len(todo) - left)
            todo = todo[:left]
    over += max(0, len(todo) - max_count)
    todo = todo[:max_count]
    todo, bodies, rejected = prepare_message_plan(todo, message_path, sender)
    for name, url, reason in rejected:
        code = "opener" if "書き出し" in reason else "adoption"
        skipped.append({"name": name, "url": url, "reason_code": code, "text": "書き出しが空です" if code == "opener" else reason, "fix": "none"})
    words = placeholders.sample_words()
    headings = placeholders.heading_words(words=words)
    keep_todo, keep_bodies, subjects, labels = [], [], [], []
    for row, body in zip(todo, bodies):
        name, url = row[0], row[1]
        subject = subject_for(name, url, sender)
        left_subject = subject_leftovers(name, url, sender, words)
        left_body = body_leftovers(body, name, row[3], words, headings)
        if left_subject:
            item = {"reason_code": "subject", "text": f"件名の「{left_subject[0]['match']}」の部分が置き換わりません", "fix": "sender"}
        elif any(w["match"] == "{資料URL}" for w in left_body) and not str(sender.get("資料URL") or "").strip():
            item = {"reason_code": "sender_url", "text": "文面に資料のリンクがありますが、送信元の「資料URL」が空です", "fix": "sender"}
        elif left_body:
            w = left_body[0]
            item = {"reason_code": "message", "text": (f"文面に見本の「{w['word']}」が残っています" if w["kind"] == "見本"
                                                       else f"文面の「{w['match']}」の行が、見本の書き込み欄のままに見えます(業種ごとの文面の見出しではありません)。中身を書き入れるか、行を消してください" if w["kind"] == "見出しでない1行"
                                                       else f"文面に置き換わらない「{w['match']}」が残っています"), "fix": "message"}
        elif not CONTACT.search(body):
            item = {"reason_code": "contact", "text": "文面に送信者の連絡先(メールか電話)が入っていません", "fix": "sender"}
        else:
            keep_todo.append(row)
            keep_bodies.append(body)
            subjects.append(subject)
            if explain:  # どの文面を使うか(画面の表示用)。本文は上の prepare_message_plan と同じ選び方で決まっている
                labels.append("承認済みの文面" if message_adoption.case_for(name, url) else pick_message_source(message_path, row[2])[0])
            continue
        skipped.append(dict(item, name=name, url=url))
    stop = "" if keep_todo else ("送れる会社がありません" if not skipped else "送れる会社がありません(理由は送らない会社の一覧)")
    return {"todo": keep_todo, "bodies": keep_bodies, "subjects": subjects, "labels": labels, "skipped": skipped,
            "digest": plan_digest(keep_todo, keep_bodies, sender) if keep_todo else None, "left_today": left, "over_limit": over, "stop": stop}


def daily_cut_explains(csv_path, include_supplemented, gate, max_count, message_path, sender, expect, left_now):
    """確認番号の不一致が「きょうの残りが減った」だけで説明できるか。止める文を原因に合わせて分けるためだけに使う(送るかどうかは変えない)。
    きょうの残りで切らない送る内容を作り、先頭から k 社で切った確認番号に expect と同じものがあれば True。
    陽性対照: 確認のあとに別の回で1社送り、残りが3→2社になった → True。誤爆の対照: 文面を変えた → False(どの k でも番号が合わない)。
    原理的に取りこぼす型: 残りが減ったうえに中身も変わったとき(False=中身が変わった、の文になる)"""
    if left_now is None:
        return False
    alt = build_plan(csv_path, include_supplemented, gate, max_count, message_path, sender, apply_daily=False)
    todo, bodies = alt["todo"], alt["bodies"]
    for k in range(1, len(todo) + 1):  # 送る内容の検査で外れた会社があるので、残りの数と k は一致しない。先頭からの全部の長さを試す
        if plan_digest(todo[:k], bodies[:k], sender) == expect:
            return True
    return False


class LogError(Exception):
    pass


STATE = {"clicked": False}  # 送信ボタンを押したあとか。押したあとの失敗は「要手動」ではなく「不明(要確認)」にする


def log_row(row):
    try:
        LOG.parent.mkdir(exist_ok=True)
        with filelock.locked(LOG):  # 新規判定・見出しの追加・行の追加を、他の実行と交ぜずに行う
            new = not LOG.exists()
            with open(LOG, "a", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(LOG_COLS)
                w.writerow(row)
    except (OSError, TimeoutError) as e:
        raise LogError(repr(e))


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def sent_urls(results_only=False):
    """送信ログに「押した」記録があるフォームの鍵。results_only=True は結果の行(送信済み・不明・入力エラー)だけ。
    押す前に書く「送信試行」の行は含めない(押していないのに残る行で、対象から永久に落とさないため)"""
    if not LOG.exists():
        return set()
    after = ("届いた(本人確認)", "届いていない(本人確認)", "手で送信済み")  # 送ったあとに本人が記録した結果(sales_records)。自動では送り直さない
    kinds = ("送信済み", "不明(要確認)", INPUT_ERROR_RESULT) + after if results_only else ("送信試行", "送信済み", "不明(要確認)", INPUT_ERROR_RESULT) + after
    with open(LOG, encoding="utf-8-sig", newline="") as f:
        return {k for r in csv.DictReader(f) if r["結果"] in kinds for k in state.keys_of([r["フォームURL"]])}


def _gate_blocked_keys(gate):
    if gate is None or not gate.db_path.exists():
        return set()
    import sqlite3
    c = sqlite3.connect(str(gate.db_path))
    try:
        return {r[0] for r in c.execute("SELECT key FROM sends WHERE state != ?", (state.PREPARED,))}
    finally:
        c.close()


def _form_value(row):
    return row.get(formlist.LEDGER_COLS[7], "")


def _target_is_ng(row, gate):
    form = _form_value(row)
    if gate is not None:
        return gate.is_ng([row.get("HP URL") or "", form])
    return ng_list.is_ng_row(row) or ng_list.is_ng(form)


def _hp_verify_skip_reason(row):
    ev = (row.get("HPの照合") or "").strip()
    if not ev:
        return "HPの照合が未確認"
    if re.fullmatch(r"地図に登録のHP（(?:電話一致|住所一致)）", ev):
        return ""
    if re.fullmatch(r"検索で発見・(?:電話一致|住所一致)：.+", ev):
        return ""
    if any(x in ev for x in ("名前のみ", "不一致", "未確認", "照合なし", "needs_review", "要確認")):
        return "HPの照合が自動送信には弱い: " + ev[:80]
    return "HPの照合が検証済みの電話一致/住所一致ではない: " + ev[:80]


def _target_skip_reason(row, include_supplemented=False, gate=None, done=None):
    if row.get(formlist.EXTRA_COLS[0]) != next(iter(formlist.ORDER)):
        return "フォーム判定がフォームありではない"
    if row.get(formlist.EXTRA_COLS[1]):
        return "営業お断り表示あり"
    if _target_is_ng(row, gate):
        return "NGリスト対象"
    if "補完" in row.get("取得元", "") and not include_supplemented:
        return "検索補完HPのため未承認"
    hp_reason = _hp_verify_skip_reason(row)
    if hp_reason:
        return hp_reason
    url = row.get(formlist.LEDGER_COLS[7], "")
    key = (state.keys_of([url]) or [""])[0]
    if not url.startswith("http"):
        return "フォームURLがhttp(s)ではない"
    if done is not None and key in done:
        return "送信済みまたは同一回の重複フォームURL"
    return ""


_FREE_MAIL = re.compile(r"@(?:gmail\.com|yahoo\.co\.jp|yahoo\.com|outlook\.\w+|hotmail\.\w+|icloud\.com|docomo\.ne\.jp|ezweb\.ne\.jp|softbank\.ne\.jp|au\.com|nifty\.com|live\.jp)$", re.I)


def _mail_of(row):
    """メールのみの行の宛先。列「メールアドレス」を優先し、無ければ連絡先の列(フォームURL無ければメール)がアドレスの形のときそれを使う。
    推測では作らない(HPに出ていたものだけ)。全角・mailto:・前後の空白をそろえる。複数並んでいるものはそのまま返す(形が不正として落ちる)"""
    m = (row.get(formlist.EXTRA_COLS[3]) or "").strip()
    if not m:
        m = _form_value(row).strip()
        if "@" not in m or m.lower().startswith("http"):
            return ""
    m = unicodedata.normalize("NFKC", m).strip()
    return m[len("mailto:"):].strip() if m.lower().startswith("mailto:") else m


def row_channel(row):
    """連絡のチャネル。フォームあり=フォーム、フォームが無く公開アドレスがある(メールのみ)=メール、どちらでもなければ なし"""
    verdict = (row.get(formlist.EXTRA_COLS[0]) or "").strip()
    if verdict == next(iter(formlist.ORDER)):
        return "フォーム"
    if verdict == "メールのみ" and _mail_of(row):
        return "メール"
    return "なし"


def _mail_root(mail):
    return site_checker._root(mail.rsplit("@", 1)[-1])


def _hp_root(row):
    host = urlparse(row.get("HP URL") or "").netloc
    return site_checker._root(host) if host else ""


def _mail_is_ng(row, mail, gate):
    """HPとアドレスの両方を見る。アドレスのドメインがNGドメインでも止める(gateの有無で結果を変えない)"""
    if gate is not None:
        return gate.is_ng([row.get("HP URL") or "", mail])
    return ng_list.is_ng_row(row) or ng_list.is_ng_email(mail) or ng_list.is_ng("https://" + mail.rsplit("@", 1)[-1] + "/")


def mail_skip_reason(row, include_supplemented=False, gate=None, done=None):
    """メール対象にならない理由。空なら対象。条件は、HPの照合が電話一致か住所一致・営業お断りの表示なし・NGリスト外(HPとアドレスの両方)・
    採用や個人情報の窓口ではない・HPに公開された自社のアドレス(フリーメール以外はHPと同じドメイン)・同じ会社に重複して送らない。
    done には、送ったアドレス(正規化)と、送った/送る会社の目印 "site:<HPの登録ドメイン>" を入れる"""
    if row_channel(row) != "メール":
        return "メールのみではない"
    mail = _mail_of(row)
    if not site_checker.EMAIL.fullmatch(mail):
        return "メールアドレスの形が不正"
    if site_checker.BAD_MAIL.search(mail):
        return "採用・個人情報などの窓口のアドレス"
    if row.get(formlist.EXTRA_COLS[1]):
        return "営業お断り表示あり"
    if (row.get("sales_permission") or "").startswith("営業不可"):
        return "営業不可候補: " + row["sales_permission"][:60]
    if _mail_is_ng(row, mail, gate):
        return "NGリスト対象"
    if "補完" in row.get("取得元", "") and not include_supplemented:
        return "検索補完HPのため未承認"
    hp_reason = _hp_verify_skip_reason(row)
    if hp_reason:
        return hp_reason
    hp_root = _hp_root(row)
    if hp_root and not _FREE_MAIL.search(mail) and hp_root != _mail_root(mail):
        return "アドレスのドメインがHPと違う(制作会社・掲載先のアドレスの疑い)"
    if done is not None:
        if ng_list.norm_email(mail) in done:
            return "送信済みまたは同一回の重複アドレス"
        if hp_root and "site:" + hp_root in done:
            return "同じ会社(HP)に、フォームかメールで送る・送った行がある"
    return ""


def _mail_scan(csv_path, include_supplemented=False, gate=None):
    """メールのみの行を1回だけ走査し、(対象, 対象外) を返す(対象と内訳を別々に数えない)。
    同じ会社へフォームとメールの両方が行かないよう、フォームで送る行・送信済みの行のHPを先に目印として入れる"""
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    form_done = _done_keys(gate)
    done = set()
    for r in rows:
        if row_channel(r) == "フォーム":
            key = (state.keys_of([r.get(formlist.LEDGER_COLS[7], "")]) or [""])[0]
            if (key in form_done or not _target_skip_reason(r, include_supplemented=include_supplemented, gate=gate, done=set())) and _hp_root(r):
                done.add("site:" + _hp_root(r))
    targets_, skips = [], []
    for r in rows:
        if (r.get(formlist.EXTRA_COLS[0]) or "").strip() != "メールのみ":
            continue
        mail = _mail_of(r)
        reason = mail_skip_reason(r, include_supplemented=include_supplemented, gate=gate, done=done)
        if reason:
            skips.append((r.get("企業名", ""), mail, reason))
            continue
        done.add(ng_list.norm_email(mail))
        if _hp_root(r):
            done.add("site:" + _hp_root(r))
        targets_.append((r.get("企業名", ""), mail, r.get("業種", ""), r.get("書き出し", ""), r.get("HP URL", "")))
    return targets_, skips


def mail_target_skip_reasons(csv_path, include_supplemented=False, gate=None):
    """メールのみの行のうち、メール対象にならないものの (企業名, アドレス, 理由)。件数の内訳に使う"""
    return _mail_scan(csv_path, include_supplemented, gate)[1]


def mail_targets(csv_path, include_supplemented=False, gate=None):
    """メール対象の (企業名, アドレス, 業種, 書き出し, HP URL)。リストの順。第2段のメール送信が使う(第1段では数えるだけで送らない)"""
    return _mail_scan(csv_path, include_supplemented, gate)[0]


def _done_keys(gate):
    """送らないフォームの鍵。画面版(gate)でも、コマンド版が送信ログに残した結果(送信済み・不明・入力エラー)を含める"""
    if gate is not None:
        return _gate_blocked_keys(gate) | sent_urls(results_only=True) | state.blocked_keys()
    return sent_urls() | state.blocked_keys()


def target_skip_reasons(csv_path, include_supplemented=False, gate=None):
    done = _done_keys(gate)
    out = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            reason = _target_skip_reason(r, include_supplemented=include_supplemented, gate=gate, done=done)
            if reason:
                out.append((r.get("企業名", ""), r.get(formlist.LEDGER_COLS[7], ""), reason))
                continue
            url = r.get(formlist.LEDGER_COLS[7], "")
            key = (state.keys_of([url]) or [""])[0]
            done.add(key)
    return out


def targets(csv_path, include_supplemented=False, gate=None):
    """検索で補ったHP(取得元に「補完」)は別の会社の可能性が残るため、明示しない限り対象にしない"""
    done = _done_keys(gate)
    out = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if _target_skip_reason(r, include_supplemented=include_supplemented, gate=gate, done=done):
                continue
            url = r.get(formlist.LEDGER_COLS[7], "")
            key = (state.keys_of([url]) or [""])[0]
            if url.startswith("http") and key not in done:
                done.add(key)  # 同じ回の中の重複(チェーンの共通フォーム)も1回にする
                out.append((r["企業名"], url, r.get("業種", ""), r.get("書き出し", ""), r.get("HP URL", "")))
    return out


def label_of(el):
    """入力欄の名前の手がかり: name/id/placeholder/aria-label/紐づくlabel/直前の文字"""
    return el.evaluate(
        """e => { const g = a => e.getAttribute(a) || ''; let t = [g('name'), g('id'), g('placeholder'), g('aria-label')];
        if (e.id) { const l = document.querySelector('label[for="' + e.id + '"]'); if (l) t.push(l.innerText); }
        let p = e.parentElement;
        for (let i = 0; p && i < 4; i++, p = p.parentElement) {
          if (p.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select').length !== 1) break;
          let s = (p.innerText || '').trim();
          if (!s && p.previousElementSibling) s = (p.previousElementSibling.innerText || '').trim();
          if (s) { t.push(s.slice(0, 60)); break; }
        }
        return t.join(' '); }""")


PREF = re.compile(r"^(北海道|東京都|(?:京都|大阪)府|.{2,3}県)")
CITY = re.compile(r"^(.+?[市区町村])(?=.)")


def split_address(addr):
    """住所を (都道府県, 市区町村, それ以降) に分ける。分けられなければ空文字で返す"""
    m = PREF.match(addr)
    if not m:
        return "", "", addr
    rest = addr[m.end():]
    c = CITY.match(rest)
    return (m.group(1), c.group(1), rest[c.end():]) if c else (m.group(1), "", rest)


def own_attrs(el):
    """その入力欄自身の name/id/placeholder だけ。周りの文字(姓と名が同じ枠に入る等)を混ぜない"""
    return el.evaluate("e => [e.name, e.id, e.placeholder].join(' ')")


def fit_length(el, value):
    """maxlengthに収まらないときは、ハイフンを除く(郵便番号・電話番号の7桁・11桁指定への対応)"""
    ml = el.evaluate("e => e.maxLength")
    return value.replace("-", "") if ml and 0 < ml < len(value) else value


def _same_value(got, want, key):
    """欄から読み戻した値が、入れた値と同じか。本文は改行コード(\\r\\n と \\n)だけを揃えて、一字一句で比べる。
    本文以外の欄は全角半角(NFKC)と空白を揃えて比べ、電話・郵便番号は数字だけで比べる(サイトの自動整形で区切りが変わっても同じ値とみなす)。
    フリガナはひらがなとカタカナの違いを見ない"""
    if key == "本文":
        return (got or "").replace("\r\n", "\n") == (want or "").replace("\r\n", "\n")

    def norm(s):
        s = re.sub(r"\s", "", unicodedata.normalize("NFKC", s or ""))
        if key in ("電話", "郵便番号"):
            s = re.sub(r"\D", "", s)
        if key == "フリガナ":
            s = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)
        return s
    return norm(got) == norm(want)


def _put(el, value, key):
    """欄に値を入れ、入った値を読み戻して入れた値と同じかを確かめる。同じなら None、違えば利用者向けの理由。
    入れる前に maxlength を見て、値がそれより長ければ入れずに止める(本文が切れて署名・配信停止の案内が落ちたまま送らないため)。
    陽性対照(test_send_flow_offline): 本文欄 maxlength=120 に約240字の本文 → 理由が返り、送信ボタンは押されない。
    誤爆の対照: maxlength の無い本文欄・十分な maxlength → None。電話欄が自動でハイフンを付けても None。
    原理的に取りこぼす型: 入れた直後は全文が入っていて、送信ボタンを押したとき(サーバー側)に切られるもの。画面に見えない別の欄へ値を写すサイトの作り"""
    want = value or ""
    try:
        ml = el.evaluate("e => e.maxLength")
    except Exception:
        ml = -1
    js_len = len(want.replace("\r\n", "\n").encode("utf-16-le")) // 2  # ブラウザの maxlength は UTF-16 の長さで数える
    if isinstance(ml, int) and 0 < ml < js_len:
        return f"「{key}」の欄が{ml}文字までで、入れる{key}({js_len}文字)が入りきらない(入力欄の文字数の上限)"
    el.fill(want)
    try:
        got = el.input_value()
    except Exception:
        return f"「{key}」の欄に入った値を読み戻せない"
    if not _same_value(got, want, key):
        return f"「{key}」の欄に入った値が、入れた値と違う(入力欄の文字数の上限などで入りきらなかった)"
    return None


SEI = re.compile(r"last|sei|family|姓|surname", re.I)
MEI = re.compile(r"first|mei|given|(?<![姓氏])名(?!前)", re.I)
ADDR_PART = re.compile(r"address|addr|住所|市区町村|番地|city|street", re.I)


NEUTRAL_OPTION = re.compile(r"^(その他|その他(の|のご)?(相談|質問|お問い?合わせ)?|まだ決まっていない(です)?|未定|特になし|ご相談|ご質問|お問い?合わせ|一般(の|のご)?(相談|質問|お問い?合わせ)?)$")


def neutral_option(el):
    """必須の選択式で、どの用件にも当てはまる「その他・まだ決まっていない・一般のお問い合わせ」系の選択肢が、ちょうど1つあるときだけ、その文言を返す。
    0個・2個以上・判断がつかないときは None(呼び出し側で「要手動」にする)"""
    try:
        opts = el.evaluate("e => Array.from(e.options).filter(o => o.value !== '' && !o.disabled).map(o => o.textContent.trim())")
    except Exception:
        return None
    hits = [o for o in opts if NEUTRAL_OPTION.match(re.sub(r"[\s　]+", "", o))]
    return hits[0] if len(hits) == 1 else None


def fill_form(page, sender, message, skipped=None):
    """戻り値: (成功, メモ)。埋められない必須項目があれば送らない。
    skipped にリストを渡すと、埋められない必須項目で止まらず、理由をそこへ集めて残りを入力する(手動経路用。送らない前提)"""
    def _cannot(reason):
        if skipped is None:
            return False, reason
        skipped.append(reason)
        return None

    name_parts = [p for p in re.split(r"[ 　]+", sender.get("氏名", "").strip()) if p]
    kana_parts = [p for p in re.split(r"[ 　]+", sender.get("フリガナ", "").strip()) if p]
    pref, city, rest_addr = split_address(sender.get("住所", ""))
    n_addr = sum(1 for e in page.locator("input:visible[type=text], input:visible:not([type])").all() if ADDR_PART.search(own_attrs(e)))
    addr_seen = 0
    if CAPTCHA.search(page.content()):
        if skipped is None:
            return False, "captchaあり"
        skipped.append("captcha(手で解いてください)")  # 手動経路は、人が解く前提で入力を続ける
    scope, err = pick_form(page)
    if scope is None:
        return False, err
    filled = []

    def put(el, value, key):
        """入れて読み戻す。違えば、本番は押さずに止める理由を返し、手動経路は未入力の一覧へ足して続ける"""
        why = _put(el, value, key)
        if why and skipped is not None:  # 手動経路は人が直す前提。入るところまで入れておき(欄の上限で切れる)、切れたことを一覧に出す
            try:
                el.fill(value or "")
            except Exception:
                pass
        return _cannot(why + "。送信ボタンは押さず、自分で送る会社へ回します" if skipped is None else why + "(手で直してください)") if why else None

    generic = []  # 欄の名前だけで割り当てた項目(同じ項目が2欄以上に入るなら、分割欄で誤入力の恐れ)
    for el in scope.locator("input:visible, textarea:visible, select:visible").all():
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        typ = (el.get_attribute("type") or "text").lower()
        if typ in ("hidden", "submit", "button", "image", "search", "password"):
            continue
        lab = label_of(el)
        required = el.get_attribute("required") is not None or "必須" in lab or "required" in lab.lower()
        if typ == "checkbox":
            if CONSENT.search(lab):
                el.check()
            elif required:
                r = _cannot("必須のチェック項目: " + lab[:30])
                if r:
                    return r
            continue
        if tag == "select" and pref and re.search(r"pref|都道府県", own_attrs(el) + lab, re.I):
            try:
                el.select_option(label=pref)
                filled.append("都道府県")
                continue
            except Exception:
                pass  # 選べなければ下の必須判定へ
        if typ in ("radio", "file") or tag == "select":
            if required and not (tag == "select" and el.input_value()):
                pick = neutral_option(el) if tag == "select" else None  # 判断がつくものだけ自動で選ぶ。つかなければ要手動
                r = None
                if pick is None:
                    r = _cannot("必須の選択項目: " + lab[:30])
                else:
                    try:
                        el.select_option(label=pick)
                        filled.append("選択:" + pick)
                    except Exception:
                        r = _cannot("必須の選択項目: " + lab[:30])
                if r:
                    return r
            continue
        if tag == "textarea":
            r = put(el, message, "本文")  # 入った値を読み戻す。欄の文字数の上限で切れたまま(署名・配信停止の案内が落ちたまま)押さない
            if r:
                return r
            filled.append("本文")
            continue
        own = own_attrs(el)
        if len(name_parts) == 2 and not re.search(r"mail|tel|company|corp", own, re.I):  # 姓・名が別々の欄
            kana = re.search(r"kana|カナ|かな|furigana|フリガナ", own, re.I)
            parts = (kana_parts if len(kana_parts) == 2 else None) if kana else name_parts  # 読みが未設定ならかなの欄に漢字を入れない
            if parts is None:
                pass
            elif SEI.search(own) and not MEI.search(own):
                r = put(el, parts[0], "氏名" if not kana else "フリガナ")
                if r:
                    return r
                filled.append("氏名" if not kana else "フリガナ"); continue
            elif MEI.search(own) and not SEI.search(own):
                r = put(el, parts[1], "氏名" if not kana else "フリガナ")
                if r:
                    return r
                filled.append("氏名" if not kana else "フリガナ"); continue
        if ADDR_PART.search(own) and pref:  # 住所が複数欄に分かれている形
            addr_seen += 1
            if n_addr >= 2:
                val = city if addr_seen == 1 and city else rest_addr if city else sender["住所"]
                r = put(el, val, "住所")
                if r:
                    return r
                filled.append("住所"); continue
        key = next((k for rx, k in FIELD_MAP if rx.search(lab)), None)
        if key and sender.get(key):
            r = put(el, fit_length(el, sender[key]) if key in ("郵便番号", "電話") else sender[key], key)
            if r:
                return r
            filled.append(key)
            generic.append(key)
        elif typ == "email" and sender.get("メール"):
            r = put(el, sender["メール"], "メール")
            if r:
                return r
            filled.append("メール")
        elif required:
            r = _cannot("必須の未知項目: " + lab[:30])
            if r:
                return r
    dup = [k for k in ("氏名", "フリガナ", "電話", "郵便番号", "会社名", "住所") if generic.count(k) > 1]
    if dup:
        return False, "同じ種類の欄が複数あり判別できない: " + "・".join(dup)
    if "本文" not in filled:
        return False, "本文を入れられない"
    return True, "入力: " + "・".join(dict.fromkeys(filled)) + (("／未入力%d件: " % len(skipped) + "、".join(skipped)) if skipped else "")


# 自動記入の対応表(正本)。フォームの項目 → sender.json のキー → 見分ける手がかり。fill_form はこの表のとおりに動く。
# 表に無い必須項目は推測で埋めず、止める(本番)か、未入力として一覧に出す(手動経路)。
COVERAGE = [
    ("メール(確認用の再入力欄を含む)", "メール", "欄のラベルに メール/mail/確認、または type=email"),
    ("フリガナ", "フリガナ", "ラベルに フリガナ/カナ。姓・名が別欄なら分けて入れる(読みが未設定なら入れない)"),
    ("会社名・団体名", "会社名", "ラベルに 会社/企業/法人/団体/company"),
    ("件名・題名", "件名", "ラベルに 件名/題名/subject"),
    ("電話番号", "電話", "ラベルに 電話/tel。桁数の制限は欄の長さに合わせる"),
    ("郵便番号", "郵便番号", "ラベルに 郵便/zip"),
    ("住所(1欄・分割欄)", "住所", "ラベルに 住所/address。都道府県のプルダウンは選ぶ。複数欄に分かれていれば市区町村・番地に分ける"),
    ("氏名(1欄・姓名の分割欄)", "氏名", "ラベルに 氏名/お名前/担当者名。姓・名が別欄なら分ける"),
    ("本文(textarea)", "(送る文面)", "textarea に画面で確認した全文を入れる"),
    ("同意チェック(個人情報・規約)", "(不要)", "ラベルに 同意/承諾/プライバシー のものだけチェックする"),
    ("必須のプルダウン", "(中立の選択肢)", "「その他」「お問い合わせ」など判断のつく選択肢だけ選ぶ。つかなければ未入力として止める"),
    ("上記以外の必須項目", "(なし)", "推測で埋めない。本番の送信は止め、手動経路では未入力として理由を出す"),
]


def coverage_text():
    return "\n".join("%s ← %s(%s)" % row for row in COVERAGE)


def prefill_manual(url, sender, message):
    """手動経路: ブラウザを開いて入力できる項目を埋めるところまで。送信ボタンは押さない。画面を閉じるまで待つ。
    戻り値: (入力メモ, 未入力の一覧)。実行環境: Windows / Mac / Linux(画面が出る環境)"""
    skipped = []
    with sync_playwright() as p:
        br = p.chromium.launch(headless=False)
        try:
            page = br.new_context().new_page()
            page.goto(url, timeout=30000)
            ok, memo = fill_form(page, sender, message, skipped)
            page.wait_for_event("close", timeout=0)  # 人が内容を確かめて、自分の手で送る。ここでは押さない
        finally:
            br.close()
    return memo, skipped


def manual_prefill_company(csv_path, sender_path, message_path, company_name):
    """画面の「手で送る」。1社のフォームを開いて入力できる項目を埋め、送信ボタンは押さずに、画面を閉じるまで待つ。"""
    sender = json.loads(Path(sender_path).read_text(encoding="utf-8-sig"))
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("企業名") == company_name]
    if len(rows) != 1:
        print("指定した会社が1行に決まらないため、手動入力を始めません")
        return 5
    r = rows[0]
    row = (r["企業名"], r.get("問い合わせフォームURL 無ければメール", ""), r.get("業種", ""), r.get("書き出し", ""), r.get("HP URL", ""))
    if not re.match(r"https?://", row[1]):
        print("この会社はフォームのURLがありません(メールのみ)。手動入力の対象外です")
        return 5
    ok_url, why_url = netguard.check_url(row[1])
    if not ok_url:
        print("宛先が公開サイトではないため開きません: " + why_url)
        return 5
    todo, bodies, rejected = prepare_message_plan([row], message_path, sender)
    if rejected or len(todo) != 1:
        print("本文を作れないため停止:", rejected)
        return 5
    eff = dict(sender, 件名=sender.get("件名", "").replace("{企業名}", row[0]))
    print(f"手動入力: {row[0]} のフォームを開きます。入力できる項目だけ自動で埋めます。送信ボタンは押しません。")
    memo, skipped = prefill_manual(row[1], eff, bodies[0])
    print("入力のメモ: " + str(memo))
    if skipped:
        print("自動で入れられなかった必須項目(手で入れてください): " + "、".join(map(str, skipped)))
    print("手動入力を終えました")
    return 0


NOT_INQUIRY_ATTR = re.compile(r"reserv|booking|review|survey|recruit|entry|newsletter|subscribe|予約|口コミ|クチコミ|アンケート|応募|エントリー|メルマガ", re.I)
INQUIRY_WORD = re.compile(r"問い?合わ?せ|contact|inquiry|enquiry|ご相談|お問合", re.I)


def pick_form(page):
    """本文欄(textarea)を持つ可視のフォームから、問い合わせ用を1つ選ぶ。戻り値: (範囲, エラー文)。
    予約・口コミ・アンケート等のフォームは選ばない。問い合わせ用が2つ以上あって決められないときも選ばない(違うフォームへ送らない)"""
    cands = {}
    for ta in page.locator("textarea:visible").all():
        form = ta.locator("xpath=ancestor::form[1]")
        if not form.count():
            cands.setdefault("page", (page, ""))
            continue
        f = form.first
        key = f.evaluate("f => { if (!f.dataset.pfk) f.dataset.pfk = String(Math.random()); return f.dataset.pfk; }")
        if key in cands:
            continue
        attrs = f.evaluate("f => [f.id, f.className, f.getAttribute('action') || ''].join(' ')")
        head = f.evaluate("f => (f.innerText || '').slice(0, 300)")
        cands[key] = (f, attrs + " " + head)
        if NOT_INQUIRY_ATTR.search(attrs):
            cands[key] = None
        elif NOT_INQUIRY_ATTR.search(head) and not INQUIRY_WORD.search(attrs + " " + head):
            cands[key] = None
    if not cands:
        return None, "本文欄が見つからない"
    ok = [v for v in cands.values() if v]
    if not ok:
        return None, "本文欄のあるフォームが予約・口コミ・アンケート用で、問い合わせ用が見つからない"
    try:
        page_ev = page.url + " " + page.title()
    except Exception:
        page_ev = page.url
    if len(ok) > 1:
        pref = [v for v in ok if v[1] and INQUIRY_WORD.search(v[1])]
        if len(pref) != 1:
            return None, "問い合わせ用の候補が複数あり決められない"
        ok = pref
    elif not INQUIRY_WORD.search((ok[0][1] or "") + " " + page_ev):  # 単独でも、問い合わせだと言える証拠(フォーム内の文言・属性・ページのURLや題名)が要る
        return None, "本文欄のあるフォームが1つあるが、問い合わせ用だと言える証拠が無い"
    return ok[0][0], ""


SEND_BTN = re.compile(r"確認|送信|送る|submit|send", re.I)
CLEAR_BTN = re.compile(r"消去|クリア|リセット|取消|取り消し|キャンセル|clear|reset|cancel", re.I)


def find_submit(page):
    scope, _ = pick_form(page)
    if scope is None:
        return None
    cands = []
    for b in scope.locator("button:visible, input[type=submit]:visible, input[type=button]:visible").all():
        label = (b.inner_text() or b.get_attribute("value") or "").strip()
        typ = (b.get_attribute("type") or "").lower()
        if typ in ("reset", "image") or BACK_BTN.search(label) or CLEAR_BTN.search(label):
            continue
        cands.append((b, label, typ))
    sends = [b for b, label, _ in cands if SEND_BTN.search(label)]
    if len(sends) == 1:
        return sends[0]
    if not sends:  # 文言で決められないときは、submit が1つだけのときに限る
        subs = [b for b, _, typ in cands if typ == "submit" or typ == ""]
        return subs[0] if len(subs) == 1 else None
    return None  # 送信らしいボタンが複数あるときは押さない


def click_submit(btn, page):
    STATE["clicked"] = True
    btn.click()
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass  # 押したあとの待ち時間切れは失敗ではない。結果は完了画面の有無で判定する
    return True


def _new_hits(pattern, body):
    """押す前の画面に無かった文言(押したあとに新しく出たもの)だけを返す。押す前の画面を取っていなければ、全部を新しいものとして扱う"""
    before = STATE.get("before_text")
    if before is None:  # 押す前の画面が無ければ「新しく出た」と言えない。完了とも入力エラーとも言わず「不明」に倒す
        return set()
    return {m.group(0) for m in pattern.finditer(body)} - {m.group(0) for m in pattern.finditer(before)}


DONE_NEG = re.compile(r"(でき|出来)(ません|なかった)|失敗|エラー|not been sent|failed|unable", re.I)


def is_done(page):
    """押したあとに新しく出た完了の文言があり、入力欄(本文)がもう無いときだけ完了とみなす。
    入力画面や確認画面のステップ表示(「入力→確認→送信完了」)・案内文の「送信完了」は、押す前から画面にあるので数えない"""
    body = page.inner_text("body")
    return bool(_new_hits(DONE, body)) and not _new_hits(DONE_NEG, body) and page.locator("textarea:visible").count() == 0 and not _submit_still_offered(page)


# 完了画面にあってよいボタン(戻る・閉じる・同意・検索・メニュー)。これ以外のボタンが見えている画面は、まだ先がある(確認画面など)とみなす
SEND_WORD = re.compile(r"送信|送る|問い?合わせる|申し?込む|確定|submit|send", re.I)
HARMLESS_BUTTON = re.compile(r"トップ|戻る|ホーム|閉じる|メニュー|検索|同意|許可|拒否|cookie|accept|close|menu|search|top|home|back|^ok$|^x$|^×$", re.I)


def _submit_still_offered(page):
    """まだ押すボタンが出ている画面は、完了画面ではない。確認画面のステップ表示の「送信完了」(ボタンの文言が「この内容で問い合わせる」のように送信の語を含まない場合)を、送信済みにしない。
    見えて押せるボタンのうち、戻る・閉じる・同意・検索・メニュー以外があれば True。画面を読めないときは False(従来の判定に任せる)。
    完了画面に別のボタンがあって完了と判定できなくなる側(誤って「不明(要確認)」になる)は、自動で再送しないので安全側"""
    try:
        labels = page.eval_on_selector_all(
            "button:not([disabled]), input[type=submit]:not([disabled]), input[type=button]:not([disabled]), [role=button]",
            "els => els.filter(e => e.offsetParent !== null).map(e => ((e.innerText || e.value || e.getAttribute('aria-label') || '') + '').trim())")
        links = page.eval_on_selector_all(  # a タグ・画像ボタンは、送信の語を含むものだけ数える(完了画面のふつうのリンクで「不明」にしない)
            "a, input[type=image]:not([disabled])",
            "els => els.filter(e => e.offsetParent !== null).map(e => ((e.innerText || e.value || e.getAttribute('alt') || e.getAttribute('aria-label') || '') + '').trim())")
    except Exception:
        return False
    return any(l and not HARMLESS_BUTTON.search(l) for l in labels) or any(l and SEND_WORD.search(l) and not BACK_BTN.search(l) for l in links)


# 送信後も本文欄が残っていて、入力エラーの表示がある。「不明」と分けて記録する(先方に届いていない可能性が高いが、自動では再送しない)
INPUT_ERROR = re.compile(r"必須項目|必須です|入力(して|を)ください|入力されていません|未入力|正しく入力|正しい形式|入力内容に(誤り|不備)|入力エラー|エラーが(あります|ございます|発生)|is required|please (enter|fill)|invalid", re.I)
INPUT_ERROR_RESULT = "入力エラー(要確認)"


SUCCESS_HINT = re.compile(r"送信(さ)?れました|送信しました|送信完了|ありがとうございました|has been sent|was sent|successfully|thank you", re.I)


def has_input_error(page):
    """送信ボタンを押したあと、本文欄がまだ見えていて、押す前には無かった入力エラーの文言が新しく出ている。
    成功らしい文言も新しく出ているとき(Contact Form 7 型の「メッセージは送信されました」で本文欄が残る等)は、
    入力エラーと言い切れないので False(「不明」に倒す)。判別できなければ False"""
    try:
        if page.locator("textarea:visible").count() == 0:
            return False
        body = page.inner_text("body")
        return bool(_new_hits(INPUT_ERROR, body)) and not _new_hits(SUCCESS_HINT, body)
    except Exception:
        return False


CONFIRM_TEXT = re.compile(r"入力内容(の)?(ご)?確認|内容(を|に)?(ご)?確認|以下の内容で|この内容で|確認画面|confirm", re.I)
BACK_BTN = re.compile(r"戻る|修正|back", re.I)


def confirm_button(page, before_url, message=""):
    """1回目のクリック後が確認画面だと言えるときだけ、2回目に押す送信ボタンを返す。言えなければ None(押さない)。
    確認画面の条件: 本文欄がもう無い(入力エラー・処理中のフォームではない) かつ 確認の文言がある かつ
    「戻る/修正」ボタンと「送信」ボタンが両方ある。入力エラーのまま同じフォームに居るときは押さない"""
    try:
        if page.locator("textarea:visible").count() != 0:
            return None
        body_text = page.inner_text("body")
        if not CONFIRM_TEXT.search(body_text):
            return None
        if _new_hits(DONE, body_text):  # 押したあとに「送信しました」等が新しく出た。受け付け済みの恐れがあるので二度押さない
            return None
        snippet = re.sub(r"\s+", "", message)[:20]
        if snippet and snippet not in re.sub(r"\s+", "", body_text):  # 確認画面なら、入れた本文がそのまま表示されている
            return None
        btns = page.locator("button:visible, input[type=submit]:visible, input[type=button]:visible, input[type=image]:visible, a:visible")
        labels = [(b, (b.inner_text() or b.get_attribute("value") or b.get_attribute("alt") or "").strip()) for b in btns.all()]
        if not any(BACK_BTN.search(t) for _, t in labels):
            return None
        sends = [b for b, t in labels if re.search(r"送信|送る|submit|send", t, re.I) and not BACK_BTN.search(t)]
        return sends[0] if len(sends) == 1 else None
    except Exception:
        return None


_SEND_GATE = None


def set_send_gate(gate):
    """送らない会社と送信状態の正本(DB)を関門にする。画面(app.py)から起動したときは必ずこれを使う(--ng-state-db)。
    None のときはテキストのNGリストと送信状態.db(コマンドから試すとき・テスト用)"""
    global _SEND_GATE
    prev = _SEND_GATE
    _SEND_GATE = gate
    return prev


def _gate_is_ng(gate, urls):
    if gate is not None:
        return gate.is_ng([u for u in urls if u])
    return any(ng_list.is_ng(u) or ng_list.is_ng_form(u) for u in urls if u)


def _gate_reserve(gate, urls, name):
    if gate is not None and hasattr(gate, "reserve_send"):
        return gate.reserve_send(urls, name)
    token = gate.reserve(urls, name) if gate is not None else state.reserve(urls, name)
    if token:
        return token, None
    return None, ("スキップ", "このフォームは別の実行が送信中か送信済み(送らない)")


def _gate_start_click(gate, urls, token):
    if gate is not None and hasattr(gate, "start_click_send"):
        return gate.start_click_send(urls, token)
    ok = gate.start_click(urls, token) if gate is not None else state.start_click(urls, token)
    if ok:
        return True, None
    return False, ("スキップ", "確保が別の実行へ引き継がれたため送らない")


def _gate_mark(gate, urls, result_state, memo, token):
    if gate is not None:
        return gate.mark(urls, result_state, memo, token)
    return state.mark(urls, result_state, memo, token)


def _gate_ng_result():
    return "要手動", "送信の直前にNGリストへ追加されていたため送らない"


def send_one(page, name, url, sender, message, do_send, hp=""):
    if do_send:
        message_adoption.validate_send(name, url, message, sender)
    else:
        message_adoption.validate_body(name, url, message, sender)
    sender = message_adoption.effective_sender(name, url, sender)
    gate = _SEND_GATE
    STATE["clicked"] = False  # 前の会社のクリックを持ち越さない(goto が失敗しても、押していない会社を「不明」にしない)
    STATE["gate"] = gate
    STATE["token"] = None
    STATE["before_text"] = None
    STATE["targets"] = [url]
    ok_url, why_url = netguard.check_url(url)
    if not ok_url:
        return "要手動", "宛先が公開サイトではないため開かない: " + why_url
    page.goto(url, timeout=30000)
    message_adoption.validate_destination(name, url, page.url)
    page.wait_for_load_state("networkidle", timeout=20000)
    refuse = site_checker.refuse_in(page.inner_text("body"))  # フォーム自体の「営業お断り」。入力より前に見る
    if refuse:
        return "お断り表示(送らない)", refuse.group(0)[:40]
    kind, kind_why = site_checker.form_kind_result(site_checker.BeautifulSoup(page.content(), "html.parser"), page.url, page.title())  # 一覧作成と同じ判定を、いま開いているフォームにも掛ける
    if kind in ("purpose", "unknown"):
        return "要手動", ("用途限定のフォーム(営業の送り先にしない)" if kind == "purpose" else "フォームの用途を判定できない") + ": " + kind_why
    if placeholders.leftovers(sender.get("件名", "").replace("{企業名}", MASK), allow_heading=False):  # 会社名(【公式】など)の部分は見ない
        return "要手動", "件名に置き換わらない仮の文字が残っている"
    sender = dict(sender, 件名=sender.get("件名", "").replace("{企業名}", name))  # 件名にも会社名を差し込む
    ok, memo = fill_form(page, sender, message)
    SHOTS.mkdir(parents=True, exist_ok=True)
    shot = SHOTS / (time.strftime("%Y%m%d_%H%M%S_") + re.sub(r'[\\/:*?"<>|#\s]', "_", name)[:30] + ".png")
    page.screenshot(path=str(shot), full_page=True)
    if not ok:
        return "要手動", memo
    if not do_send:
        return "試走OK", memo + "（送信ボタンは押していない）"
    btn = find_submit(page)
    if btn is None:
        return "要手動", "送信ボタンが見つからない"
    if FIRST_LOOK["pending"]:  # はじめての人の1社目: 入力と画面の保存のあと、押す前に人に見せる。この下の見直し(NG・送信済み・上限)は待ったあとに行う
        FIRST_LOOK["pending"] = False
        if not first_look_wait(name, shot, first_look_fields(sender, message)):
            return sales_records.FIRST_LOOK_STOPPED, memo + "（1社目の入力した画面を見て止めたか、5分のあいだ選ばれなかったため、送信ボタンは押していない）"
    late = send_rules.window_refusal()  # 1社目の確認を待つあいだ(最大5分)や、ページを開くあいだに送れる時間を過ぎていたら押さない
    if late:
        return "要手動", late + "(送れる時間を過ぎたため、送信ボタンは押していない。次の平日の時間内に送れます)"
    if _gate_is_ng(gate, [url, page.url, hp]):  # 長い送信の途中で追加されたNGも、押す直前に見る(会社のHPのドメインも)
        return _gate_ng_result()
    if set(state.keys_of([url, page.url])) & (sent_urls(results_only=True) | state.blocked_keys()):  # 画面版・コマンド版のどちらで押したものも、送信ログの結果行で止める
        return "スキップ", "このフォームは送信ログに結果(送信済み・不明・入力エラー)があるため送らない"
    db = gate.db_path if gate is not None else state.DB  # 1日の上限の枠は、送信状態と同じDBで数える(同時に動く別の実行と共有する)
    slot = send_rules.reserve_daily(db, LOG, name, url)
    if slot is None:
        return DAILY_FULL, f"1日の上限({send_rules.DAILY_LIMIT}社)に達したため送らない(同時に動いている別の実行の分を含む)"
    try:
        return _press(page, name, url, message, hp, gate, btn, memo, shot)
    finally:
        send_rules.settle_daily(db, slot, STATE["clicked"])  # 押さずに終わった枠は戻し、押した(押したかもしれない)枠は数え続ける


DAILY_FULL = "上限(送らない)"
FIRST_LOOK_SECONDS = 300  # 1社目の確認を待つ長さ。長く待つとフォームの有効期限が切れるので5分で止める
FIRST_LOOK = {"pending": False}  # main が --first-look(はじめての人の本番)のとき True。押す直前まで来た最初の1社だけで待ち、待ったら False


def first_look_fields(sender, message):
    """1社目の確認で見せる値。フォームに入れるために渡した値(画面から読み戻した値ではない)と、本文の先頭3行"""
    rows = [{"label": k, "value": str(sender.get(k))} for k in ("会社名", "氏名", "フリガナ", "メール", "電話", "住所", "件名") if sender.get(k)]
    return rows + [{"label": "本文(先頭3行)", "value": "\n".join((message or "").strip().split("\n")[:3])}]


def first_look_wait(name, shot, fields, timeout=None, interval=2.0):
    """はじめての人の1社目で、入力した画面を人に見せ、「このまま送る」が選ばれたときだけ True。
    起動元の画面(app.py)に POST /api/firstlook/wait {company, shot(画像のファイル名), fields, deadline} を interval 秒ごとに送り、
    応答 {"go": true} で True、{"go": false} で False、{"go": null} は待ち続ける。期限(既定 FIRST_LOOK_SECONDS)を過ぎたら False。
    画面から起動していない(ポートの環境変数が無い)ときは、誰も答えられないので False(押さない)。
    陽性対照(test_send_flow_offline): 答えない画面・「止める」と答える画面・画面なし → False で、送信ボタンは押されない。誤爆の対照: 「送る」と答える画面 → True。
    原理的に取りこぼす型: 画面を操作できるAIや人が、中身を読まずに「このまま送る」を押すこと(承認ダイアログと同じ限界)
    待ちの受付番号: 呼ばれるたびに新しい番号(wait)を作って送り、{"go": true} は応答の wait が同じときだけ受け付ける(前の待ち・別の会社への答えで押さない)。
    期限を過ぎてから届いた {"go": true} は受け付けない(問い合わせの応答が遅れて期限をまたいだ場合)。
    陽性対照: wait の無い/違う {"go": true} → 待ち続けて期限で False。期限1秒・応答の遅れ2.5秒の {"go": true} → False"""
    import approval
    import secrets
    import urllib.request
    port = os.environ.get(approval.PORT_ENV, "")
    if not port.isdigit():
        return False
    deadline = time.time() + (FIRST_LOOK_SECONDS if timeout is None else timeout)
    wait_id = secrets.token_hex(8)
    payload = json.dumps({"company": name, "shot": Path(shot).name, "fields": fields, "deadline": int(deadline), "wait": wait_id}, ensure_ascii=False).encode("utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.time() < deadline:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/firstlook/wait", data=payload, method="POST",
                                     headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{port}", "X-Token": os.environ.get(approval.UI_TOKEN_ENV, "")})
        try:
            with opener.open(req, timeout=max(0.5, min(10, deadline - time.time()))) as r:
                ans = json.loads(r.read().decode("utf-8"))
            go = ans.get("go")
            if go is True and ans.get("wait") != wait_id:  # 今の待ちへの答えでない「送る」は数えない
                go = None
        except Exception:  # 画面が一時的に答えないときは、期限まで問い合わせ続ける(押さない側に倒れる)
            go = None
        if go is True:
            return time.time() <= deadline  # 期限を過ぎてから届いた「送る」では押さない
        if go is False:
            return False
        time.sleep(interval)
    return False


def _press(page, name, url, message, hp, gate, btn, memo, shot):
    token, refusal = _gate_reserve(gate, [url, page.url], name)
    if not token:  # 別のプロセスが同じフォームを送信中か送信済み
        return refusal
    STATE["token"] = token
    log_row([now(), name, url, "送信試行", "送信ボタンを押す直前に記録(二重送信を防ぐ)"])  # 書けなければここで止まり、送らない(状態は「準備」のまま=押していない)
    targets_now = [url, page.url]
    STATE["targets"] = targets_now
    if _gate_is_ng(gate, targets_now + [hp]):  # ログ書き込みの間に追加されたNGも、押す直前にもう一度見る
        return _gate_ng_result()
    started, refusal = _gate_start_click(gate, targets_now, token)
    if not started:  # ログ書き込み等に時間がかかり、確保が別の実行へ引き継がれていたら押さない
        return refusal
    before = page.url
    try:
        STATE["before_text"] = page.inner_text("body")  # 押す前の画面。押したあとに新しく出た文言だけを完了・入力エラーの根拠にする
    except Exception:
        STATE["before_text"] = None
    click_submit(btn, page)
    note = ""
    nxt = confirm_button(page, before, message)  # 確認画面の判定を、完了の判定より先に行う(確認画面の案内文の「送信完了」を完了にしない)
    stuck = False
    if nxt is not None and _gate_is_ng(gate, targets_now + [page.url, hp]):  # 確認画面にいるあいだに別経路で追加されたNGも、本送信の直前に見る
        final = ("要手動", memo + " 確認画面の送信ボタンを押す直前にNGリストへ追加されていたため、2回目(本送信)を押していない")
        _gate_mark(gate, targets_now, final[0], final[1], token)
        return final
    if nxt is not None:  # 確認画面だと識別できたときだけ、もう一段押す
        try:
            STATE["before_text"] = page.inner_text("body")  # 確認画面の案内文・ステップ表示も「押す前」に含める
        except Exception:
            STATE["before_text"] = None
        nxt.click()
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        if confirm_button(page, before, message) is not None:  # 押したのに確認画面のまま(確認ダイアログ・同意未チェック・CAPTCHA等)
            stuck = True
            note = " 確認画面の送信ボタンを押したが確認画面のまま"
    elif not is_done(page):
        note = " 確認画面かどうか判別できず、再クリックしていない"
    page.screenshot(path=str(shot.with_name(shot.stem + "_送信後.png")), full_page=True)
    if not stuck and is_done(page):
        final = ("送信済み", memo)
    elif not stuck and has_input_error(page):
        final = (INPUT_ERROR_RESULT, memo + " 送信後もフォームが残り、入力エラーの表示がある。先方に届いていない可能性が高いが、自動では再送しない" + note)
    else:
        final = ("不明(要確認)", memo + " 完了画面の文言を確認できず" + note)
    _gate_mark(gate, targets_now, final[0], final[1], token)
    return final


BROWSER_MAX_BYTES = 30_000_000  # ブラウザ側の1通信の上限。申込フォームのページとして十分な大きさ


def block_internal(route):
    """ブラウザが出す全ての通信を、1件ずつ確かめてから通す。
    宛先の名前を引いて、内部・私設のアドレス(localhost.・nip.io 型の名前・古い書き方のIP・IPv6 埋め込みを含む)なら止める。引けない名前も止める。
    転送(3xx)はここでは自動で辿らず、転送先を確かめてからブラウザに返す(ブラウザは転送先へ改めて出すので、そこでもこの関門を通る)"""
    url = route.request.url
    if not url.startswith(("http://", "https://")):
        return route.continue_()  # data: blob: about: など。ネットワークには出ない
    ok, _why = netguard.check_url_cached(url, strict=True)
    if not ok:
        return route.abort()
    try:
        resp = route.fetch(max_redirects=0)
    except Exception:
        return route.abort()
    try:
        if int(resp.headers.get("content-length", "0")) > BROWSER_MAX_BYTES:
            return route.abort()  # 巨大な応答はメモリに溜めない
    except ValueError:
        return route.abort()
    loc = resp.headers.get("location")
    if 300 <= resp.status < 400 and loc:
        from urllib.parse import urljoin
        ok2, _why2 = netguard.check_url_cached(urljoin(url, loc), strict=True)
        if not ok2:
            return route.abort()
    route.fulfill(response=resp)


def block_ws(ws):
    """WebSocket はこの道具の用途で要らない。何もしない=サーバーへつながない(つなぐ処理を書かなければ、ページ側の接続は実在せず、どこにも出ない。
    ws.close() を呼ぶと同期 API では止まるので呼ばない。実測: 登録しないと内部サーバーに /ws が届く)"""
    return None


def plain_failure(e):
    """押す前に止まった理由を、利用者の言葉と原文で"""
    raw = repr(e)[:120]
    if "Timeout" in raw:
        return "ページを開けないか、時間内に表示されませんでした。手で開いて送ってください。原文: " + raw
    if "ERR_NAME_NOT_RESOLVED" in raw or "ERR_CONNECTION" in raw or "net::" in raw:
        return "このページにつながりませんでした(アドレスが変わったか、サイトが止まっている)。原文: " + raw
    return "途中で止まりました。手で開いて送ってください。原文: " + raw


def positive_int(s):
    """--max は1以上の整数だけ。負数は Python のスライスで「最後の1社以外」になり、大量送信になる"""
    if not re.fullmatch(r"\d+", s.strip()) or int(s) < 1:
        raise argparse.ArgumentTypeError(f"--max は1以上の整数で指定してください(指定: {s})")
    return int(s)


def check_adopted_message(csv_path, sender_path, message_path, company_name):
    """本文だけを検査。NG・送信履歴・企業適格性は未検証。ブラウザ/DBには触れない。"""
    sender = json.loads(Path(sender_path).read_text(encoding="utf-8-sig"))
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("企業名") == company_name]
    if len(rows) != 1:
        print("指定企業が1行に確定しないため本文検査を停止")
        return 5
    r = rows[0]
    row = (r["企業名"], r.get("問い合わせフォームURL 無ければメール", ""), r.get("業種", ""), r.get("書き出し", ""), r.get("HP URL", ""))
    try:
        if message_adoption.case_for(row[0], row[1]) is None:
            raise message_adoption.AdoptionError("採用記録のない企業は本文検査対象外")
        todo, bodies, rejected = prepare_message_plan([row], message_path, sender)
        if rejected or len(todo) != 1:
            print("本文生成を停止:", rejected)
            return 5
        body = bodies[0]
        effective = message_adoption.effective_sender(row[0], row[1], sender)
        print("本文検査のみ・企業適格性未検証・送信しません")
        print("件名: " + effective.get("件名", "").replace("{企業名}", row[0]))
        print(body)
        print("採用本文・差込・送信者照合: PASS")
        message_adoption.validate_send(row[0], row[1], body, sender)
        print("実送信承認あり（検査終了・送信動作なし）")
        return 0
    except message_adoption.AdoptionError as exc:
        print("停止:", str(exc))
        return 4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--sender", required=True)
    ap.add_argument("--message", required=True)
    ap.add_argument("--send", action="store_true", help="本番送信。付けなければ試走")
    ap.add_argument("--preview", action="store_true", help="送る対象と各社の文面の全文を出して終わる(送らない・ブラウザも開かない)")
    ap.add_argument("--max", type=positive_int, default=20)
    ap.add_argument("--expect", help="確認(--preview)が出した確認番号。本番で送る会社・全文・送信元が、これと違えば送らずに止める")
    ap.add_argument("--include-supplemented", action="store_true", help="検索で補ったHPの会社も対象にする(別会社の恐れあり)")
    ap.add_argument("--ng-state-db", help="Explicit isolated NG/state DB used by the UI handoff; omitted keeps the normal text NG/state defaults.")
    ap.add_argument("--manual-company", help="画面の「手で送る」。この会社のフォームを開いて入力だけ自動で行う(送信ボタンは押さない)")
    ap.add_argument("--check-message-name", help="採用済み企業1社の本文検査のみ。NG/履歴/企業適格性は未検証。DB・ブラウザ・送信動作なし")
    ap.add_argument("--first-look", action="store_true", help="はじめての人の本番。押す直前まで来た1社目だけ、入力した画面を画面(app.py)で見せ、「このまま送る」が選ばれるまで押さない(5分で止める)")
    a = ap.parse_args()
    FIRST_LOOK["pending"] = False
    if a.send:  # 画面を通らない直接実行でも、利用許可の確認を通す(本番送信だけ。試走・確認は止めない)
        import license_check
        ok, why = license_check.gate()
        if not ok:
            print(why)
            return 7
    if a.manual_company:
        if a.send or a.preview:
            ap.error("--manual-company は --send/--preview と併用できません")
        return manual_prefill_company(a.csv, a.sender, a.message, a.manual_company)
    if a.check_message_name is not None:
        if not a.check_message_name.strip():
            ap.error("--check-message-name に企業名が必要です")
        if a.send or a.preview or a.ng_state_db:
            ap.error("--check-message-name は --send/--preview/--ng-state-db と併用できません")
        return check_adopted_message(a.csv, a.sender, a.message, a.check_message_name)
    gate = None
    if a.ng_state_db:
        gate = NgStateStore(a.ng_state_db)
        set_send_gate(gate)
    sender = json.loads(Path(a.sender).read_text(encoding="utf-8-sig"))
    problems = send_rules.sender_problems(sender)  # 画面・保存・MCP と同じ関門(欄の名前つき)。文面が使う資料URLは、下の build_plan が会社ごとに見る
    if problems:
        print("送信元に、送る前に直すところがあります: " + " / ".join(p["text"] for p in problems))
        return 1
    FIRST_LOOK["pending"] = bool(a.first_look and a.send)
    plan = build_plan(a.csv, a.include_supplemented, gate, a.max, a.message, sender, apply_rules=bool(a.send or a.preview))
    for s in plan["skipped"]:
        print(f"送らない会社: {s['name']}  {s['text']}")
    if plan["left_today"] == 0 and plan["stop"]:
        print(plan["stop"])
        return 6
    todo, bodies = plan["todo"], plan["bodies"]
    print(f"対象 {len(todo)}社（フォームあり・お断り表示なし・送信ログに無い{'・検索で補ったHPを含む' if a.include_supplemented else '・検索で補ったHPは除く'}）")
    if not todo:
        return 0
    got = plan["digest"]
    if a.send and a.expect and got != a.expect:  # 確認のあとで、送る会社・文面・送信元が変わった。確認していない相手に送らない
        if daily_cut_explains(a.csv, a.include_supplemented, gate, a.max, a.message, sender, a.expect, plan["left_today"]):
            print(f"送る内容を出したあとで、きょう送れる残りが減りました(別の送信が先に進んだため。いまの残りは{plan['left_today']}社)。"
                  "送る会社・文面・送信元は変わっていません。送らずに止めます。もう一度「送る内容を出す」を押します")
        else:
            print("送る内容を出したあとで、送る会社・文面・送信元のどれかが変わりました。送らずに止めます。もう一度「送る内容を出す」を押します")
        return 4
    if a.send:
        try:
            for row, body in zip(todo, bodies):
                message_adoption.validate_send(row[0], row[1], body, sender)
        except message_adoption.AdoptionError as exc:
            print(str(exc))
            return 4
    legal = send_rules.legal_missing(sender)
    if a.preview and legal:
        print("注意: 署名に次の表示がありません。このままでは本番で送れません(特定電子メール法): " + "、".join(legal))
    if a.preview:
        for (name, url, ind, opener, _hp), body in zip(todo, bodies):
            print(f"\n===== 送る文面 {name}（業種「{ind or '指定なし'}」）=====")
            print("件名: " + message_adoption.effective_sender(name, url, sender).get("件名", "").replace("{企業名}", name))
            print(body + "=====")
        print(f"\n送信者: {sender['会社名']} {sender['氏名']} {sender['メール']}\n以上 {len(todo)}社が送信対象です（まだ送っていません）。")
        print(f"確認番号: {got}")
        return 0
    if a.send:
        # 本番は、ツールの画面で人が承認したあとに発行される使い切りの鍵が要る。コマンドの直接実行では送れない
        import approval
        if legal:
            print("署名に次の表示がないため、送りません(特定電子メール法): " + "、".join(legal) + "。送信元の署名を直してから、もう一度「確認する」からやり直してください")
            return 1
        late = send_rules.window_refusal()
        if late:
            print(late)
            return 6
        if not (a.expect and approval.consume_ticket(approval.env_token(), a.expect)):
            print("本番の送信は、ツールの画面（または AI から formlist_send）で、人が全文を確認して承認したあとにだけ始められます。このコマンドからは送れません")
            return 5
        for (name, url, ind, opener, _hp), body in zip(todo, bodies):
            print(f"\n===== 送る文面 {name}（業種「{ind or '指定なし'}」）=====")
            print("件名: " + message_adoption.effective_sender(name, url, sender).get("件名", "").replace("{企業名}", name))
            print(body + "=====")
        print(f"送信者: {sender['会社名']} {sender['氏名']} {sender['メール']}\n{len(todo)}社へ送ります。")
        if input("送ってよければ「送信」と入力してください > ").strip() != "送信":
            print("中止しました")
            return 0
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        ctx = page = None
        results = []
        for i, (name, url, ind, opener, hp) in enumerate(todo, 1):
            if a.send:  # 1社ごとに、時刻・1日の上限・同じ会社の間隔をもう一度見る(待ち時間や並行実行の間に変わるため)
                stop = send_rules.window_refusal()
                if not stop and send_rules.pressed_today(LOG) >= send_rules.DAILY_LIMIT:
                    stop = f"1日の上限({send_rules.DAILY_LIMIT}社)に達しました"
                if stop:
                    print(f"{stop}。残りの会社には送らず、ここで止めます")
                    break
                again = send_rules.company_refusal(LOG, name, url, hp)
                if again:
                    print(f"[{i}/{len(todo)}] 同じ会社のため送りません: {name}  {again}")
                    continue
            STATE["clicked"] = False
            STATE["gate"] = _SEND_GATE
            STATE["token"] = None
            STATE["targets"] = [url]
            body = bodies[i - 1]
            if body_leftovers(body, name, opener):  # 置き換わらない記号・見本の仮の文字が残った文面は送らない(build_plan で外しているが、押す前にもう一度見る)
                res, memo = "要手動", "文面に置き換わらない仮の文字が残っているので送らない(フォームは開いていない)"
            elif not CONTACT.search(body):  # 送信者の連絡先が最終の本文に無い(署名の設定は有っても、文面から消えている)
                res, memo = "要手動", "文面に送信者の連絡先(メールか電話)が入っていない"
            else:
                try:
                    if ctx is not None:
                        ctx.close()  # 会社ごとに新しい状態で始める(cookie・保存された下書き・同意状態を次の会社へ持ち越さない)
                    ctx = br.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36", service_workers="block")
                    ctx.route("**/*", block_internal)
                    ctx.route_web_socket("**/*", block_ws)
                    page = ctx.new_page()
                    res, memo = send_one(page, name, url, sender, body, a.send, hp)
                except LogError as e:  # 記録が書けないまま送ると、次の回にもう一度送る。ここで止める
                    print("送信ログに書けません（Excelで開いていませんか）。二重送信を防ぐため、ここで止めます:", e)
                    br.close()
                    return 2
                except Exception as e:  # 1社の失敗で止めない。原文を残す
                    res, memo = ("不明(要確認)", "送信ボタンを押した後に止まりました。届いたかは手で確かめてください。原文: " + repr(e)[:90]) if STATE["clicked"] else ("要手動", plain_failure(e))
                    if STATE["clicked"] and STATE.get("token"):
                        gate = STATE.get("gate")
                        targets_for_mark = STATE.get("targets") if gate is not None else [url]
                        _gate_mark(gate, targets_for_mark or [url], res, memo, STATE["token"])
            results.append(res)
            print(f"[{i}/{len(todo)}] {res}  {name}  {memo}")
            try:
                log_row([now(), name, url, res, memo])
            except LogError as e:
                print("送信ログに書けません（Excelで開いていませんか）。二重送信を防ぐため、ここで止めます:", e)
                br.close()
                return 2
            if res == DAILY_FULL:
                print("1日の上限に達したため、残りの会社には送らず、ここで止めます")
                break
            if res == sales_records.FIRST_LOOK_STOPPED:
                print("1社目の確認で止めたため、残りの会社には送らず、ここで止めます")
                break
            if res.startswith("お断り"):
                if ng_list.add(hp or url, "営業お断り表示(フォーム画面)「" + memo[:30] + "」", name):
                    print("  営業お断りの会社として NGリストに追加しました")
                if gate is not None:
                    gate.add_domain_ng(hp or url, "営業お断り表示(フォーム画面)", name)
            elif res == "要手動" and ("captcha" in memo or "本文欄が見つからない" in memo):  # 一時的な失敗(Timeout等)は入れない
                if ng_list.add_form(url, "自動で送れない: " + memo[:20], name):
                    print("  このフォームをNGリストに追加しました（今後は一覧に出しません）")
                if gate is not None:
                    gate.add_form_ng(url, "自動で送れない", name)
            if a.send and i < len(todo):
                time.sleep(random.uniform(30, 60))
        br.close()
    c = Counter(results)
    print(f"\n結果の集計（全{len(results)}社中）: " + " / ".join(f"{k} {v}社（{100 * v // len(results)}%）" for k, v in c.items()))
    print(f"\n記録: {LOG}\n画面の保存: {SHOTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


