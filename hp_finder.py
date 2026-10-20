"""地図にホームページが載っていない会社の、公式HPを検索で補う。実行環境: Windows / Python 3.9+。
Bingの検索結果ページ(GETのみ・1件ずつ間隔をあけて)から、予約サイト・口コミ・SNS・地図などを除いた最上位の結果を採る。
採った結果が正しい会社かは保証できない(チェーン店は本部のHPが出る)。そのため呼び出し側は「検索で補った」と明記し、
会社名の最初の塊と過半の塊が結果の題名か説明に無い結果は採らない(same_company)。
対照(導入時に実測): 東横INN門前仲町永代橋 -> toyoko-inn.com / コンフォートホテル東京清澄白河 -> choice-hotels.jp。
原理的に取りこぼす型: 名前が一般名詞だけの会社(「ホテル」「TAKASHO Stay」等)、検索結果が全て予約サイトの会社、検索側の制限(429/チャレンジ画面)。
その場合は空を返し、呼び出し側が「HP不明」として残す。"""
import base64
import json
import random
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

import hp_verify

HERE = Path(__file__).resolve().parent
CACHE = HERE / "出力" / "hp_cache.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Accept-Language": "ja-JP,ja;q=0.9"}
SKIP = re.compile(
    r"(wikipedia|jalan|rakuten|booking\.com|tripadvisor|ikyu|expedia|agoda|trivago|google|yahoo|bing|microsoft|"
    r"facebook|instagram|twitter|(^|\.)x\.com|youtube|tiktok|line\.me|note\.com|ameblo|goo\.ne|mapion|navitime|ekiten|"
    r"tabelog|hotpepper|gurunavi|retty|carsensor|weblio|indeed|townpage|itp\.ne|mapfan|jtb\.co|knt\.co|"
    r"relux|yadoplaza|tabikobo|hotels\.com|kayak|skyscanner|airbnb|hostelworld|zenrin|cybozu|baidu|wikiwand|kotobank|"
    r"minimo|rekubi|ozmall|epark|airrsv|coubic|salonboard|cocoloni|ekiten)", re.I)


def _norm(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s)).lower()


LEGAL = re.compile(r"株式会社|有限会社|合同会社|一般社団法人|一般財団法人|公益社団法人|公益財団法人|社会福祉法人|医療法人|学校法人|\(株\)|\(有\)")
GENERIC = {"ホテル", "旅館", "店", "本店", "支店", "ショップ", "センター", "ホーム",
           "特別養護老人", "特別養護老人ホーム", "老人ホーム",
           "美容室", "美容院", "サロン", "ヘアサロン", "ネイル", "ネイルサロン", "エステ", "エステサロン", "カフェ", "整体院", "クリニック", "歯科"}  # どの会社にも付く語。これだけでは同じ会社と言えない


def _tokens(name, area=""):
    """会社名を文字種の塊に割る: 東横INN門前仲町永代橋 -> [東横, inn, 門前仲町永代橋]。1文字・法人の種類・一般的な語は捨てる"""
    n = _norm(LEGAL.sub("", unicodedata.normalize("NFKC", name)))
    a = _norm(area)
    return [t for t in re.findall(r"[ァ-ヶー]+|[一-龥々]+|[a-z0-9]+|[ぁ-ん]+", n) if len(t) >= 2 and t not in GENERIC and not (a and t in a)]


def same_company(name, text, area=""):
    """最初の塊が結果に在り、かつ塊の半分以上が在る。ファミリーロッジ旅籠屋 -> ファミリーマートを弾くための条件"""
    toks = _tokens(name, area)
    text = _norm(text)
    return bool(toks) and toks[0] in text and sum(t in text for t in toks) * 2 >= len(toks)


def _decode(href):
    href = href.replace("&amp;", "&")
    if "bing.com/ck/a" in href:
        u = parse_qs(urlparse(href).query).get("u", [""])[0]
        if u.startswith("a1"):
            s = u[2:] + "=" * (-len(u[2:]) % 4)
            try:
                return base64.urlsafe_b64decode(s).decode("utf-8", "replace")
            except ValueError:
                return ""
    return href


def _line_id(row):
    if row.get("_budget_line_id"):
        return str(row["_budget_line_id"])
    return "|".join(str(row.get(k, "")) for k in ("source_key", "name", "area", "tel", "address"))


def _claim(budget, line_id):
    if budget:
        budget.claim(line_id or "hp-search")


def _bing(q, budget=None, line_id=None):
    """Bingの検索結果から (URL, 題名と説明の文字列) を上から返す。予約サイト・口コミ・SNSなどは除く。通信の失敗は例外のまま上げる"""
    _claim(budget, line_id)
    r = requests.get("https://www.bing.com/search", params={"q": q, "setlang": "ja", "cc": "JP"}, headers=UA, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"検索がHTTP {r.status_code}（回数制限の可能性）")
    if 'id="b_results"' not in r.text:  # 確認画面などで結果欄が無い。「HPが無い」と記録して保存しない
        raise RuntimeError("検索結果の欄が読めません（確認画面の可能性）")
    out = []
    for b in re.findall(r'<li class="b_algo".*?</li>', r.text, re.S):
        m = re.search(r'<h2[^>]*><a[^>]*href="([^"]+)"', b)
        if not m:
            continue
        url = _decode(m.group(1))
        if not url.startswith("http") or SKIP.search(urlparse(url).netloc):
            continue
        out.append((url.split("?msockid")[0], _norm(re.sub(r"<[^>]+>", "", b))))
    return out


RULES = "v9"  # 照合の規則(hp_verify・search_one)を変えたら必ず上げる。古い規則で採った結果をキャッシュから再利用しないため
MAX_CANDIDATES = 4  # 照合のために開く候補の数(1候補あたりトップページ1枚)


def check_candidate_for_search(url, row, candidate_allowed=None, budget=None, line_id=None):
    return hp_verify.check_candidate_detail(url, row, url_allowed=candidate_allowed, budget=budget, line_id=line_id)


def search_one(row, candidate_allowed=None, budget=None, line_id=None):
    """戻り値: (url, 証拠レベル, 理由)。見つからなければ ('', レベル, 理由)。通信の失敗は例外のまま上げる。
    候補は上から順に、電話・住所を照合して最初に強い証拠が出たものを採る(電話・住所は検索語でなく、見つけたページの中身との照合に使う)。
    強い証拠が無いときは、名前が十分に固有な会社に限り、題名に会社名がある最初の候補を「名前のみ」で採る"""
    name, area = row["name"], row["area"]
    line_id = line_id or _line_id(row)
    queries = [(f"{name} {area} 公式サイト", True)]  # 電話番号での検索は使わない(2026-10-09 実測: 結果が「03」の解説ページばかりで、電話を含む結果は0件)
    hosts, tested, fallback = set(), 0, None
    row.pop("_candidate_verify_detail", None)
    row["_candidate_verify_details"] = []
    for i, (q, prefilter) in enumerate(queries):
        if i:
            time.sleep(random.uniform(1.2, 2.2))
        for url, text in _bing(q, budget=budget, line_id=line_id):
            host = urlparse(url).netloc
            if host in hosts or (prefilter and not same_company(name, text, area)):
                continue
            if candidate_allowed and not candidate_allowed(url):
                row["_candidate_verify_details"].append({"candidate_url": url, "level": "不一致", "why": "blocked by candidate policy", "category": "blocked"})
                continue
            hosts.add(host)
            if tested >= MAX_CANDIDATES:
                break
            tested += 1
            try:
                lvl, why, detail = check_candidate_for_search(url, row, candidate_allowed=candidate_allowed, budget=budget, line_id=line_id)
            except hp_verify.HttpBudgetExceeded:
                row["_candidate_verify_details"].append({"candidate_url": url, "level": "取得不可", "why": "http_budget_exceeded", "category": "fetch_failed"})
                raise
            row["_candidate_verify_details"].append(detail)
            if lvl in hp_verify.STRONG:
                row["_candidate_verify_detail"] = detail
                return url, lvl, why  # 強い証拠が出たら、残りの候補も次の検索もしない
            if fallback is None and hp_verify.usable(row, lvl):
                row["_candidate_verify_detail"] = detail
                fallback = (url, lvl, why)
    if fallback:
        return fallback
    return "", "不一致", (f"候補{tested}件のどれも、電話・住所・会社名が合わない" if tested else "検索結果に候補が無い")


def _save_cache(cache):
    """複数の検索が同時に走っても、互いの結果を消さない。書く直前に最新のファイルを読み、自分の分を重ねてから置き換える"""
    import filelock
    CACHE.parent.mkdir(exist_ok=True)
    with filelock.locked(CACHE):
        try:
            disk = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
        except ValueError:
            disk = {}
        disk.update(cache)
        filelock.atomic_write(CACHE, json.dumps(disk, ensure_ascii=False, indent=0), encoding="utf-8")


def fill(rows, log, pause=(1.2, 2.2), want=None, candidate_allowed=None, budget=None):
    """HPが空の行にURLを入れる。戻り値: (補えた件数, 試した件数)。結果は出力/hp_cache.json に保存して再利用。
    want を渡すと、補えた件数がそれに届いた時点で検索を止める(上限の何倍もの会社を検索しないため)。
    入れたHPは row["hp_ev"](証拠レベル)と row["hp_ev_why"] に印を付ける。取得元の欄には、証拠が強くても弱くても
    「Bing検索でHPを補完(要確認)」と書く。この印が自動送信の対象から外す関門で、照合の結果は「HPの照合」の列に出すだけ"""
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    except ValueError:
        cache = {}  # 壊れていたら作り直す(検索をやり直すだけ)
    found = tried = 0
    total = sum(1 for r in rows if not r["url"])
    try:
        for r in rows:
            if r["url"]:
                continue
            k = f'{RULES}|{r["name"]}|{r["area"]}|{r.get("industry", "")}|{hp_verify.tel_digits(r.get("tel", ""))}|{hp_verify.addr_key(r.get("address", ""))}'
            if want and found >= want:
                log(f"  HPが{found}件分かったので、残りの検索は止めます（上限の件数を調べるのに十分なため）")
                break
            if k not in cache:
                tried += 1
                try:
                    cache[k] = list(search_one(r, candidate_allowed=candidate_allowed, budget=budget))
                except (requests.RequestException, RuntimeError) as e:
                    log(f"  HP検索を中断します（{e}）。ここまでの結果は使います")
                    break
                time.sleep(random.uniform(*pause))
                if tried % 10 == 0:  # 途中で止まっても検索結果を失わず、進み具合が見えるようにする
                    log(f"  HP検索 {tried}件目まで済み（対象 {total}件・1件あたり十数秒かかります）")
                    _save_cache(cache)
            url, lvl, why = cache[k]
            if url and candidate_allowed and not candidate_allowed(url):
                continue
            if url:
                r["url"], r["hp_ev"], r["hp_ev_why"] = url, lvl, why
                r["source"] += " + Bing検索でHPを補完(要確認)"  # 電話・住所が合っても印は外さない(送信対象にするかは「検索で見つけたHPの会社にも送る」の選択)
                found += 1
    finally:
        _save_cache(cache)
    return found, tried
