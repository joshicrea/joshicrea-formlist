"""探しはじめる前の見積もり(Googleマップの検索が何件・何分か)。画面と MCP が使う。
formlist.py(openpyxl などを読み込むため起動に十数秒かかる)を読み込まずに答えるため、軽いモジュールに分けてある。
実行環境: Windows / Python 3.9+ / 標準ライブラリ + このフォルダの industries.py・subareas.py"""
import math

import industries
import subareas

PARALLEL_MAX = 5  # Googleマップへの同時アクセスの上限。増やすほどブロックされやすい
SECONDS_PER_CARD = 1.5 + 0.8  # map_browser_source の PAUSE + DETAIL_SETTLE_MS。所要時間の目安だけに使う
CARDS_PER_SEARCH = 15  # 暫定(未確認): 1検索で実際に開く店の数の目安。--limit は上限であって、地図の結果は小さな町では10件前後で尽きる(実ログ「候補10件のうち取得10件」)
SECONDS_PER_SEARCH = 6  # 暫定: 1検索ごとのページ読み込み・スクロールの待ち

MAP_TERMS = {n: d["map"] for n, d in industries.INDUSTRIES.items()}


def map_terms(ind):
    """業種(広い分類)で地図を引く言葉。一覧に無い言葉は「・」で分けた語をそれぞれ探す"""
    return MAP_TERMS.get(industries.canonical(ind)) or ind.split("・")


def estimate_minutes(n_searches, limit, workers):
    per_search = SECONDS_PER_SEARCH + min(limit, CARDS_PER_SEARCH) * SECONDS_PER_CARD
    return max(1, round(n_searches * per_search / max(1, min(PARALLEL_MAX, workers)) / 60))


def plan_searches(area, inds, kws, limit, workers=3):
    """始める前の見積もり。Googleマップの検索が何件(検索語×町名)になり、地図だけで何分かかるか"""
    inds = [industries.canonical(i) for i in (inds or []) if not industries.retired_reason(i)]
    subs = subareas.split(area) if area else []
    terms = [t for k in (kws or []) for t in [k]] + [t for i in inds for t in map_terms(i)]
    n = len(dict.fromkeys((t, sub) for t in terms for sub in subs))
    return {"searches": n, "minutes": estimate_minutes(n, limit, workers) if n else 0, "areas": len(subs), "terms": len(dict.fromkeys(terms)),
            "whole_area": len(subs) == 1}


# --- 件数の意味(2026-11-02 20:56 に運用者が確定した仕様) -------------------------------------------
# 出力する会社の数(limit) = 送れる会社(問い合わせフォームかメールアドレスが見つかった会社)の目標数。その数がそろうまで探す・調べるを続ける。
# 検索する数の上限(fetch_limit) = 候補を集めて調べる総量の上限(安全弁)。
DEFAULT_TARGET = 10  # 両方の欄が空欄のときの目標数
CAND_PER_SENDABLE = 3  # 暫定(実測の根拠なし): 送れる会社1社を得るのに必要な候補の数の見込み。小分けの1回に集める候補の数を決めるだけで、結果の件数には影響しない
CARDS_MIN, CARDS_MAX = 5, 20  # 地図1検索で読む店の数の下限・上限


def _int_or_none(v):
    try:
        n = int(float(v)) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def normalize_counts(limit, fetch_limit):
    """(出力する会社の数, 検索する数の上限, 画面に出す注記)を返す。
    片方が空欄(None・0・空文字)ならもう一方に合わせる。両方空欄なら10。検索する数の上限が出力する数より小さいときは、出力する数に合わせる(注記を返す)。
    空欄を200のような大きな既定値には変えない"""
    lim, fl = _int_or_none(limit), _int_or_none(fetch_limit)
    if lim is None and fl is None:
        return DEFAULT_TARGET, DEFAULT_TARGET, ""
    if fl is None:
        return lim, lim, ""
    if lim is None:
        return fl, fl, ""
    if fl < lim:
        return lim, lim, f"検索する数の上限（{fl}）が出力する会社の数（{lim}）より小さいため、検索する数の上限を{lim}に合わせて始めます"
    return lim, fl, ""


def round_plan(remaining, parallel, searches_left):
    """小分けの1回で、地図を何件ずつ読み・何本同時に始めるか。「目標まであと何社か(remaining)」で決める(無条件に同じ本数を始めない)。
    戻り: {"read_limit": 1検索で読む店の数, "searches": この回に始める検索の数, "workers": 同時に動かす本数}"""
    want = max(1, remaining) * CAND_PER_SENDABLE
    per = min(CARDS_MAX, max(CARDS_MIN, want))
    n = max(1, min(searches_left, math.ceil(want / per)))
    return {"read_limit": per, "searches": n, "workers": max(1, min(parallel, PARALLEL_MAX, n))}
