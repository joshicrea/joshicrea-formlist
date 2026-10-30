"""送ったあとの記録(営業部の後工程)。実行環境: Windows / Python 3.9+
  ・届いたかの確定: 「送れたか確認が要る」会社を、本人が自動返信メールや送信後の画面で確かめた結果で「届いた/届いていない」にする
  ・返事の記録: 会社ごとに、返事の種類・日付・原文を残す。「不要」は同じ操作で送らないリストに入れる(特定電子メール法の記録)
  ・手で送る一式: 自動で送れなかった会社に、会社ごとに置き換えた全文とフォームのアドレスを出す。手で送ったら記録する
  ・報告: 期間内の送った社数・届いた確認・返事の数と種類・確かめが要る会社・14日たって返事の無い会社
記録は 出力/送信ログ.csv(送った記録)と 出力/反応記録.csv(返事)。どちらもこのパソコンの中だけ。
守備範囲: 返事はメールを自動で読まない(本人かAIが記録する)。届いたかは本人の確認であって、先方が読んだかは分からない。
"""
import csv
import datetime as dt
from pathlib import Path

import filelock
import send_rules

HERE = Path(__file__).resolve().parent
REPLY_LOG = HERE / "出力" / "反応記録.csv"
REPLY_COLS = ["日時", "企業名", "フォームURL", "種類", "原文", "メモ"]
KINDS = ("興味あり", "資料がほしい", "金額を知りたい", "時期未定", "担当違い", "不要", "その他")
DELIVERED, NOT_DELIVERED, MANUAL_SENT = "届いた(本人確認)", "届いていない(本人確認)", "手で送信済み"
UNSURE = ("不明(要確認)", "送信試行", "入力エラー(要確認)")
FIRST_LOOK_STOPPED = "1社目の確認で止めた"  # はじめての人の1社目で、入力した画面を見て「送らずに止める」を選んだか、5分選ばれなかった。送信ボタンは押していない。
# send_rules.PRESSED に入れない(1日の上限・同じ会社の間隔に数えない)。送信ログの結果の行にも数えない(autosend.sent_urls)ので、あとで送り直せる
NEXT_STEP = {  # 返事の種類ごとの、部長(利用者のClaude)が次にすること。営業の手順書「問い合わせ後の流れ」の型に合わせる
    "興味あり": "その日のうちに、お礼と日程の候補(3つ)を返す。08 の返し方を使う",
    "資料がほしい": "その日のうちに、資料のURLかPDFを送り、日程の候補を1行添える",
    "金額を知りたい": "金額の目安と、内容で変わる点を返す。商品メニューと価格表(03)を使う",
    "時期未定": "お礼を返し、いつごろ改めて連絡してよいかを聞く",
    "担当違い": "お礼を返し、担当の方の窓口を教えてもらう",
    "不要": "これ以上送らない(送らないリストに入れた)。返信はしなくてよい",
    "その他": "原文を読んで、利用者に返し方を相談する",
}


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _read(p):
    try:
        with open(p, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _append(p, cols, row):
    p.parent.mkdir(parents=True, exist_ok=True)
    with filelock.locked(p):
        new = not p.exists()
        with open(p, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(cols)
            w.writerow(row)


def final_states(log):
    """会社(会社名とフォームのアドレス)ごとの、最後の結果"""
    out = {}
    for r in _read(log):
        out[(r.get("企業名", ""), r.get("フォームURL", ""))] = r
    return out


def unsure(log):
    """押したかもしれないが、届いたか確かめていない会社"""
    return [{"company": k[0], "form": k[1], "result": r.get("結果"), "when": r.get("日時"), "memo": r.get("メモ", "")}
            for k, r in final_states(log).items() if r.get("結果") in UNSURE]


def set_outcome(log, name, url, delivered, how=""):
    """届いた/届いていないを確定させる。送信ログに無い会社は断る(記録の無い会社を「届いた」にしない)"""
    if (name, url) not in final_states(log):
        return {"error": "送信の記録にない会社です。会社名とフォームのアドレスを、formlist_log に出ているとおりに入れてください"}
    res = DELIVERED if delivered else NOT_DELIVERED
    _append(log, ["日時", "企業名", "フォームURL", "結果", "メモ"], [_now(), name, url, res, ("確かめ方: " + how) if how else ""])
    note = "" if delivered else "自動では送り直しません。手で送る場合は formlist_manual_kit で全文を出し、送ったら formlist_manual_sent で記録します"
    return {"ok": True, "result": res, "note": note}


def manual_sent(log, name, url, how=""):
    """手で送ったことを記録する(二重送信の防止と、1日の上限・同じ会社の決まりに数える)"""
    _append(log, ["日時", "企業名", "フォームURL", "結果", "メモ"], [_now(), name, url, MANUAL_SENT, how])
    return {"ok": True, "result": MANUAL_SENT}


def add_reply(name, url, kind, text, memo=""):
    if kind not in KINDS:
        return {"error": "種類は次のどれかで入れてください: " + "、".join(KINDS)}
    _append(REPLY_LOG, REPLY_COLS, [_now(), name, url, kind, (text or "")[:2000], memo])
    return {"ok": True, "kind": kind, "next": NEXT_STEP[kind]}


def report(log, days=7, now=None):
    """期間内の営業の報告。分母が小さいうちは比率を出さず、件数だけ出す"""
    now = now or dt.datetime.now()
    since = now - dt.timedelta(days=days)

    def recent(r):
        t = send_rules._when(r)
        return t is not None and t >= since

    rows = [r for r in _read(log) if recent(r)]
    pressed = {(r["企業名"], r["フォームURL"]) for r in rows if r.get("結果") in send_rules.PRESSED}
    count = {}
    for r in rows:
        count[r.get("結果", "")] = count.get(r.get("結果", ""), 0) + 1
    replies = [r for r in _read(REPLY_LOG) if recent(r)]
    kinds = {}
    for r in replies:
        kinds[r["種類"]] = kinds.get(r["種類"], 0) + 1
    return {"period_days": days, "since": since.strftime("%Y-%m-%d"), "pressed_companies": len(pressed), "results": count,
            "delivered_confirmed": sum(1 for r in final_states(log).values() if r.get("結果") == DELIVERED and recent(r)),
            "replies": len(replies), "reply_kinds": kinds, "need_check": unsure(log), "followups": followups(log, now=now),
            "note": "件数が少ないうちは割合を出していません。届いたかは本人の確認で、先方が読んだかは分かりません"}


def followups(log, now=None):
    """送って14日たち、返事の記録が無い会社。別の窓口(電話・メール)へ1回まで連絡してよい(09 送信ルール2)。ここでは一覧を出すだけで送らない"""
    now = now or dt.datetime.now()
    replied = {r["企業名"] for r in _read(REPLY_LOG)}
    out = []
    for (name, url), r in final_states(log).items():
        t = send_rules._when(r)
        if r.get("結果") in (send_rules.PRESSED + (DELIVERED,)) and t and now - t >= dt.timedelta(days=send_rules.REPEAT_DAYS) and name not in replied:
            if send_rules.company_refusal(log, name, url, now=now) is None:
                out.append({"company": name, "form": url, "sent": r.get("日時")})
    return out
