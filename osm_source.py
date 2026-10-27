"""Company list source: OpenStreetMap (Nominatim for area, Overpass for POIs).

Runtime: Windows / cross-platform, Python 3.9+.
Source interface (so Google Places can be added later):
    fetch(area_name: str, industry: str, log) -> list[dict]
    each dict: name, industry, area, url, source, osm_ref
"""
import re
import time

import requests

import industries

UA = "AiMarunageEigyoTool/1.0 (small business lead list; respects rate limits)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# 業種の定義は industries.py(広い分類ごとの地図の検索語・OpenStreetMapの条件)。ここは Overpass の条件だけを取り出す。
INDUSTRY_TAGS = {name: d["osm"] for name, d in industries.INDUSTRIES.items()}

KW_BAD = '.^$*+?(){}[]|\\"'
URL_KEYS = ("website", "contact:website", "url", "brand:website")


def resolve_area(area_name, log):
    """Area name -> Overpass area id. Returns (area_id, display_name) or (None, reason)."""
    params = {"q": area_name, "format": "jsonv2", "limit": 5, "countrycodes": "jp", "accept-language": "ja"}
    r = requests.get(NOMINATIM, params=params, headers={"User-Agent": UA}, timeout=30)
    time.sleep(1.1)  # Nominatim policy: max 1 request / second
    r.raise_for_status()
    for hit in r.json():
        if hit.get("osm_type") == "relation":
            return 3600000000 + int(hit["osm_id"]), hit.get("display_name", area_name)
    return None, "エリア名が見つかりませんでした（市区町村名まで入れてください。例: 東京都世田谷区）"


def _overpass(query, log):
    """A 0 result is accepted only when two servers agree (a server without area data answers 0)."""
    last, zeros = None, 0
    for url in OVERPASS_URLS:
        for attempt in range(2):
            try:
                r = requests.post(url, data={"data": query}, headers={"User-Agent": UA}, timeout=120)
                if r.status_code == 200:
                    data = r.json()
                    # Overpass answers 200 with a "remark" when the query timed out: not a real 0 result
                    if "error" not in data.get("remark", "") and "timed out" not in data.get("remark", ""):
                        if data.get("elements"):
                            return data["elements"]
                        zeros += 1
                        if zeros >= 2:
                            return []
                        log("  0件だったので、別の地図サーバーでも確かめます")
                        break
                    last = f"server remark: {data['remark'][:150]}"
                else:
                    last = f"HTTP {r.status_code} {r.text[:80]!r}"
            except requests.RequestException as e:
                last = repr(e)
            log(f"  地図データの取得を再試行します（{last}）")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"地図データ(Overpass)の取得に失敗: {last}")


def fetch(area_name, industry, log):
    industry = industries.canonical(industry) if industries.canonical(industry) in INDUSTRY_TAGS else industry
    if industry not in INDUSTRY_TAGS:
        raise ValueError(f"地図で探せない業種です: {industry}")
    area_id, disp = resolve_area(area_name, log)
    if area_id is None:
        raise ValueError(disp)
    log(f"  エリア: {disp}")
    body = "".join(f"{sel}(area.a);" for sel in INDUSTRY_TAGS[industry])
    query = f"[out:json][timeout:110];area({area_id})->.a;({body});out tags center;"
    return _rows(_overpass(query, log), industry, area_name)


def fetch_keyword(area_name, keyword, log):
    """会社名・施設名に keyword を含むものを探す(大文字小文字は区別しない)。業種の欄には keyword を入れる。
    地図に載っている名前だけが対象で、事業内容では探せない"""
    keyword = keyword.strip()
    if not keyword or len(keyword) > 30:
        raise ValueError("キーワードは1〜30文字で入れてください")
    area_id, disp = resolve_area(area_name, log)
    if area_id is None:
        raise ValueError(disp)
    log(f"  エリア: {disp}")
    if any(c in KW_BAD for c in keyword):  # 地図検索の正規表現で特別な意味を持つ記号は受け付けない
        raise ValueError(f"キーワードに記号は使えません: {keyword}")
    kw = keyword
    flag = ",i" if re.search(r"[A-Za-z]", kw) else ""  # 大文字小文字を区別しない検索は重く、サーバーが504を返しやすい。英字を含むときだけ使う
    body = f'nwr["name"~"{kw}"{flag}](area.a);'
    query = f"[out:json][timeout:110];area({area_id})->.a;({body});out tags center;"
    return _rows(_overpass(query, log), keyword, area_name)


def _rows(elements, industry, area_name):
    rows = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name") or tags.get("name:ja") or tags.get("brand")
        if not name:
            continue
        url = next((tags[k] for k in URL_KEYS if tags.get(k)), "")
        addr = tags.get("addr:full") or ""  # 丁目・番・号が別々のタグで区切りなしに連結すると隣の番地と取り違えるので、まとまった住所だけ使う
        rows.append({
            "tel": tags.get("phone") or tags.get("contact:phone") or "",
            "address": addr,
            "name": name,
            "industry": industry,
            "area": area_name,
            "url": url.strip(),
            "source": "OpenStreetMap",
            "osm_ref": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
            "tags": {k: tags[k] for k in ("brand", "brand:wikidata", "operator:wikidata", "wikidata", "rooms", "stars", "building:levels") if k in tags},  # 規模の目安に使う(size_guess)
        })
    return rows
