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
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path


import message_adoption
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


def pick_message(path, industry):
    """--message がフォルダなら 業種名.txt を優先し、無ければ default.txt。ファイルならそれを使う"""
    p = Path(path)
    if p.is_dir():
        # 業種は、書かれたとおりの名前 → 新しい分類の名前(古い名前で作ったリストも、新しい分類の文面を使う) の順に探す
        import industries
        for name in dict.fromkeys([industry, industries.canonical(industry) if industry else industry]):
            f = p / f"{name}.txt"
            # CSVの業種は外部由来。「../」や絶対パスで文面フォルダの外を読ませない(区切り文字を含むか、解決後に直下でなければ default.txt)
            safe = name and not re.search(r'[\\/:*?"<>|]|\.\.', name) and f.resolve().parent == p.resolve()
            if safe and f.exists():
                return f.read_text(encoding="utf-8-sig")
        return (p / "default.txt").read_text(encoding="utf-8-sig")
    return p.read_text(encoding="utf-8-sig")


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


def fill_form(page, sender, message):
    """戻り値: (成功, メモ)。埋められない必須項目があれば送らない"""
    name_parts = [p for p in re.split(r"[ 　]+", sender.get("氏名", "").strip()) if p]
    kana_parts = [p for p in re.split(r"[ 　]+", sender.get("フリガナ", "").strip()) if p]
    pref, city, rest_addr = split_address(sender.get("住所", ""))
    n_addr = sum(1 for e in page.locator("input:visible[type=text], input:visible:not([type])").all() if ADDR_PART.search(own_attrs(e)))
    addr_seen = 0
    if CAPTCHA.search(page.content()):
        return False, "captchaあり"
    scope, err = pick_form(page)
    if scope is None:
        return False, err
    filled = []
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
                return False, "必須のチェック項目: " + lab[:30]
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
                if pick is None:
                    return False, "必須の選択項目: " + lab[:30]
                try:
                    el.select_option(label=pick)
                except Exception:
                    return False, "必須の選択項目: " + lab[:30]
                filled.append("選択:" + pick)
            continue
        if tag == "textarea":
            el.fill(message)
            filled.append("本文")
            continue
        own = own_attrs(el)
        if len(name_parts) == 2 and not re.search(r"mail|tel|company|corp", own, re.I):  # 姓・名が別々の欄
            kana = re.search(r"kana|カナ|かな|furigana|フリガナ", own, re.I)
            parts = (kana_parts if len(kana_parts) == 2 else None) if kana else name_parts  # 読みが未設定ならかなの欄に漢字を入れない
            if parts is None:
                pass
            elif SEI.search(own) and not MEI.search(own):
                el.fill(parts[0]); filled.append("氏名" if not kana else "フリガナ"); continue
            elif MEI.search(own) and not SEI.search(own):
                el.fill(parts[1]); filled.append("氏名" if not kana else "フリガナ"); continue
        if ADDR_PART.search(own) and pref:  # 住所が複数欄に分かれている形
            addr_seen += 1
            if n_addr >= 2:
                val = city if addr_seen == 1 and city else rest_addr if city else sender["住所"]
                el.fill(val); filled.append("住所"); continue
        key = next((k for rx, k in FIELD_MAP if rx.search(lab)), None)
        if key and sender.get(key):
            el.fill(fit_length(el, sender[key]) if key in ("郵便番号", "電話") else sender[key])
            filled.append(key)
            generic.append(key)
        elif typ == "email" and sender.get("メール"):
            el.fill(sender["メール"])
            filled.append("メール")
        elif required:
            return False, "必須の未知項目: " + lab[:30]
    dup = [k for k in ("氏名", "フリガナ", "電話", "郵便番号", "会社名", "住所") if generic.count(k) > 1]
    if dup:
        return False, "同じ種類の欄が複数あり判別できない: " + "・".join(dup)
    if "本文" not in filled:
        return False, "本文を入れられない"
    return True, "入力: " + "・".join(dict.fromkeys(filled))


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
    sender = dict(sender, 件名=sender.get("件名", "").replace("{企業名}", name))  # 件名にも会社名を差し込む
    if re.search(r"[{｛][^}｝]{1,20}[}｝]|【", sender["件名"]):
        return "要手動", "件名に差し込みの取り残しがある"
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
    ap.add_argument("--check-message-name", help="採用済み企業1社の本文検査のみ。NG/履歴/企業適格性は未検証。DB・ブラウザ・送信動作なし")
    a = ap.parse_args()
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
    miss = [k for k in ("会社名", "氏名", "メール", "署名") if not sender.get(k) or "【" in sender[k]]
    if not re.search(r"@|\d{2,4}-\d{2,4}-\d{3,4}", sender.get("署名", "")):
        miss.append("署名に連絡先(メールか電話)")
    if miss:
        print("sender.json に必要な項目がないか、見本のままです:", miss)
        return 1
    todo = targets(a.csv, a.include_supplemented, gate=gate) if gate is not None else targets(a.csv, a.include_supplemented)
    if a.send or a.preview:  # 同じ会社へ送りすぎない(09 送信ルール2)。確認と本番で同じ絞り方にする(確認番号を合わせるため)
        kept = []
        for row in todo:
            why = send_rules.company_refusal(LOG, row[0], row[1], row[4])
            if why:
                print(f"同じ会社のため対象外: {row[0]}  {why}")
            else:
                kept.append(row)
        todo = kept
        left = send_rules.DAILY_LIMIT - send_rules.pressed_today(LOG)
        if left <= 0:
            print(f"きょうはすでに{send_rules.DAILY_LIMIT}社に送っています(1日の上限)。あした以降に送ってください")
            return 6
        if len(todo) > left:
            print(f"1日の上限({send_rules.DAILY_LIMIT}社)まで残り{left}社のため、{left}社に絞ります")
            todo = todo[:left]
    todo = todo[: a.max]
    print(f"対象 {len(todo)}社（フォームあり・お断り表示なし・送信ログに無い{'・検索で補ったHPを含む' if a.include_supplemented else '・検索で補ったHPは除く'}）")
    if not todo:
        return 0
    todo, bodies, rejected = prepare_message_plan(todo, a.message, sender)  # 文面を先に固定。空書出しは会社単位で除外
    for name, _url, reason in rejected:
        print(f"文面未検証・対象外: {name}  {reason}")
    if rejected:
        print(f"文面生成可能 {len(todo)}社 / 空書き出しで対象外 {len(rejected)}社")
    if not todo:
        return 0
    got = plan_digest(todo, bodies, sender)
    if a.send and a.expect and got != a.expect:  # 確認のあとで、送る会社・文面・送信元が変わった。確認していない相手に送らない
        print("確認のあとで、送る会社・文面・送信元のどれかが変わりました。送らずに止めます。もう一度「確認する」からやり直してください")
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
            if re.search(r"[{｛][^}｝]{1,20}[}｝]|【", body):  # 差し込みが残った文面は送らない
                res, memo = "要手動", "文面に差し込みの取り残しがある"
            elif not re.search(r"@|\d{2,4}-\d{2,4}-\d{3,4}", body):  # 送信者の連絡先が最終の本文に無い(署名の設定は有っても、文面から消えている)
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


