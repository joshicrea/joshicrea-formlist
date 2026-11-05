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
KINDS = ("興味あり", "資料がほしい", "金額を知りたい", "時期未定", "担当違い", "不要", "その他", "商談した", "契約した")
ACTION_KINDS = ("興味あり", "資料がほしい", "金額を知りたい", "時期未定", "担当違い", "その他")  # 最後の返事がこれなら「返事に対応する」に出す。不要・商談した・契約した は対応が済んだ扱い
LOG_COLS = ["日時", "企業名", "フォームURL", "結果", "メモ", "文面名"]
RATE_MIN_SENT = 30  # 送った社数がこれ未満の文面は返事の割合を出さない(1社の返事で割合が大きく動く。値は独断なので、変えるときはこの1か所)
DELIVERED, NOT_DELIVERED, MANUAL_SENT = "届いた(本人確認)", "届いていない(本人確認)", "手で送信済み"
UNSURE = ("不明(要確認)", "送信試行", "入力エラー(要確認)")
NEXT_STEP = {  # 返事の種類ごとの、部長(利用者のClaude)が次にすること。営業の手順書「問い合わせ後の流れ」の型に合わせる
    "興味あり": "その日のうちに、お礼と日程の候補(3つ)を返す。08 の返し方を使う",
    "資料がほしい": "その日のうちに、資料のURLかPDFを送り、日程の候補を1行添える",
    "金額を知りたい": "金額の目安と、内容で変わる点を返す。商品メニューと価格表(03)を使う",
    "時期未定": "お礼を返し、いつごろ改めて連絡してよいかを聞く",
    "担当違い": "お礼を返し、担当の方の窓口を教えてもらう",
    "不要": "これ以上送らない(送らないリストに入れた)。返信はしなくてよい",
    "その他": "原文を読んで、利用者に返し方を相談する",
    "商談した": "商談の日程と内容を残す。次の手(見積・提案)を決める",
    "契約した": "契約の内容を残す。お礼を返し、始めるまでの流れを伝える",
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


def widen_log(path):
    """古い見出し(文面名の列が無い)の送信ログに「文面名」の列を足す。呼ぶ側が filelock を持っている前提。
    行は消さず、足りない列は空で埋める。書き換えは一時ファイルに書いてから入れ替える(途中で止まっても元のログが残る)"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows or "文面名" in rows[0]:
        return False
    head = rows[0] + ["文面名"]
    tmp = Path(str(path) + ".widen")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(head)
        for r in rows[1:]:
            w.writerow(r + [""] * (len(head) - len(r)))
    tmp.replace(path)
    return True


def message_names(log):
    """会社名ごとの、最後に記録された文面名(空の行は飛ばす)。手で記録した行(文面名なし)があっても、送ったときの文面名を引き継ぐ"""
    out = {}
    for r in _read(log):
        n = (r.get("文面名") or "").strip()
        if n:
            out[r.get("企業名", "")] = n
    return out


def latest_replies():
    """会社名ごとの、最後の返事(日時の古い順に読んで後勝ち)"""
    out = {}
    for r in _read(REPLY_LOG):
        out[r.get("企業名", "")] = r
    return out


def aftercare(log, now=None, days=7):
    """送ったあとの画面(送信の記録)に要るものを1回で返す: 最後の返事・「返事に対応する」会社・追いかける会社・期間内の「N社に送り、M社から返事」"""
    now = now or dt.datetime.now()
    latest = latest_replies()
    rep = report(log, days, now=now)
    return {"replies": [{"company": k, "kind": v.get("種類", ""), "when": v.get("日時", ""), "text": v.get("原文", ""), "memo": v.get("メモ", "")} for k, v in latest.items()],
            "action_needed": [k for k, v in latest.items() if v.get("種類") in ACTION_KINDS],
            "followups": rep["followups"], "summary": rep["summary"]}


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
    names = {c for c, _u in pressed}
    answered = names & {r.get("企業名", "") for r in _read(REPLY_LOG)}  # 期間内に送った会社のうち、(いつでも)返事を記録した会社。不要の返事も返事に数える
    kind_by_company = {r.get("企業名", ""): r.get("種類", "") for r in _read(REPLY_LOG)}  # 会社ごとの最後の種類
    label = message_names(log)
    by_message = {}
    for c in names:
        m = by_message.setdefault(label.get(c) or "文面名なし(記録を始める前の送信)", {"sent": 0, "replied": 0})
        m["sent"] += 1
        m["replied"] += c in answered
    for m in by_message.values():
        m["rate"] = round(m["replied"] / m["sent"], 2) if m["sent"] >= RATE_MIN_SENT else None  # 分母が少ないうちは率を出さない
    summary = {"days": days, "sent_companies": len(names), "replied_companies": len(answered),
               "meetings": sum(1 for c in names if kind_by_company.get(c) == "商談した"), "contracts": sum(1 for c in names if kind_by_company.get(c) == "契約した"),
               "by_message": by_message, "rate_min_sent": RATE_MIN_SENT,
               "rate": round(len(answered) / len(names), 2) if len(names) >= RATE_MIN_SENT else None}
    return {"period_days": days, "since": since.strftime("%Y-%m-%d"), "pressed_companies": len(pressed), "results": count,
            "delivered_confirmed": sum(1 for r in final_states(log).values() if r.get("結果") == DELIVERED and recent(r)),
            "replies": len(replies), "reply_kinds": kinds, "need_check": unsure(log), "followups": followups(log, now=now), "summary": summary,
            "note": f"送った会社が{RATE_MIN_SENT}社に届くまで、返事の割合は出していません。届いたかは本人の確認で、先方が読んだかは分かりません"}


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
