"""探しはじめる前の見積もり(Googleマップの検索が何件・何分か)。画面と MCP が使う。
formlist.py(openpyxl などを読み込むため起動に十数秒かかる)を読み込まずに答えるため、軽いモジュールに分けてある。
実行環境: Windows / Python 3.9+ / 標準ライブラリ + このフォルダの industries.py・subareas.py"""
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
    inds = [industries.canonical(i) for i in (inds or [])]
    subs = subareas.split(area) if area else []
    terms = [t for k in (kws or []) for t in [k]] + [t for i in inds for t in map_terms(i)]
    n = len(dict.fromkeys((t, sub) for t in terms for sub in subs))
    return {"searches": n, "minutes": estimate_minutes(n, limit, workers) if n else 0, "areas": len(subs), "terms": len(dict.fromkeys(terms)),
            "whole_area": len(subs) == 1}
