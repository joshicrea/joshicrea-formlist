"""地図検索の町名分割と、入力の地名の読み取り。

Googleマップの1検索は、スクロールしても最後まで出ない（区全体の「美容室 東京都江東区」は57件で止まった）。
区や市を町名ごとに分けて検索し、取りこぼしを減らす。表に無い地域は分割せず、取りこぼしの警告だけを出す。

入力の書き方のゆれ(2026-10-21 破壊レポート 致命傷4・弱点8):
  区切り 「、」「,」「・」「/」、空白(「江東区 墨田区」)、区市町村のあとの「と」「及び」「や」(「江東区と墨田区」)
  「東京都 江東区」「大阪市 北区」の空白は区切りにしない(都道府県だけ・政令市の区はつなげる)
  「東京23区」「23区」は23の区に分ける
  ヶ/ケ・ヵ/カ は、エリア外の判定のときだけ同じ字として比べる(茅ヶ崎市と茅ケ崎市、鎌ケ谷市と鎌ヶ谷市)
  都道府県を引き継ぐのは区だけ(「東京都港区、横浜市」の横浜市に東京都を付けない)
エリア外の判定は、住所の都道府県も見る(「東京都港区」の指定で「大阪府大阪市港区」を残さない)。
見ていないもの: 市の名前からの都道府県の推定(表を持たない)、町名・番地まで入れた地名の町名単位の判定
"""
import re
import unicodedata

SUBAREAS = {
    "東京都江東区": [
        "門前仲町", "木場", "清澄白河", "豊洲", "有明", "東雲", "辰巳", "潮見", "塩浜", "枝川",
        "越中島", "福住", "東陽町", "南砂町", "砂町", "亀戸", "大島", "西大島", "住吉", "森下",
        "猿江", "扇橋", "平野", "白河", "深川", "佐賀", "永代", "富岡", "牡丹", "新木場",
        "青海", "千石", "千田", "東砂", "北砂", "新砂", "常盤", "高橋", "三好", "毛利",
    ],
}

TOKYO_23 = ["千代田区", "中央区", "港区", "新宿区", "文京区", "台東区", "墨田区", "江東区", "品川区", "目黒区", "大田区", "世田谷区",
            "渋谷区", "中野区", "杉並区", "豊島区", "北区", "荒川区", "板橋区", "練馬区", "足立区", "葛飾区", "江戸川区"]

_PREF = re.compile(r"^(北海道|東京都|(?:京都|大阪)府|.{2,3}県)")
_PREF_IN = re.compile(r"(北海道|東京都|京都府|大阪府|[^\s\d〒,、()（）\-]{2,3}県)")  # 住所の中の都道府県(先頭とは限らない。郵便番号のあと)
_SEP = re.compile(r"[、,，/／・;；]+|(?<=[区市町村])(?:と|及び|および|や)(?=\S)")
_TOKYO23 = re.compile(r"(?:東京都?)?(?:23|二十三)区(?:内|全域|全部)?")
_CITY = re.compile(r"([^区]+?市(?!市)(?:[^\d市区町村]{1,5}区)?|.+?[区町村郡])")  # 四日市市・町田市・大阪市北区を、市(と区)までで切る
_ENDS_UNIT = re.compile(r"[区市町村郡]$")


def fold(s):
    """比べるためだけの正規化。全角半角をそろえ、ヶ/ケ・ヵ/カを同じ字にする(検索語には使わない)"""
    return unicodedata.normalize("NFKC", s or "").replace("ヶ", "ケ").replace("ヵ", "カ").replace("ゖ", "ケ").replace("ゕ", "カ")


def _city(rest):
    m = _CITY.match(rest)
    return m.group(1) if m else ""


def _words(chunk):
    """空白で分けた語を、1地域ずつにまとめ直す。都道府県だけの語・町名(区市町村で終わらない語)・政令市の区は前の語につなげる"""
    out = []
    for w in chunk.split():
        if out:
            prev = out[-1]
            prefecture_only = bool(_PREF.fullmatch(prev))
            town = not _ENDS_UNIT.search(w)
            ward_of_city = prev.endswith("市") and w.endswith("区") and "市" not in w and not _PREF.match(w)
            if prefecture_only or town or ward_of_city:
                out[-1] = prev + (" " if prev[-1].isascii() and prev[-1].isalnum() and w[0].isascii() and w[0].isalnum() else "") + w  # 英字の地名(New York)の空白は残す
                continue
        out.append(w)
    return out


def _parse(area):
    """戻り: [{"part": 検索に使う地名, "pref": 都道府県, "unit": 市区町村, "note": 利用者への注意}]"""
    norm = unicodedata.normalize("NFKC", area or "").strip()
    norm = re.sub(r"東京都?\s+(?=(?:23|二十三)区)", "東京", norm)  # 「東京 23区」の空白で、東京と23区を分けない
    pieces = []
    for chunk in _SEP.split(norm):
        pieces += _words(chunk or "")
    items, pref, city_prefix = [], "", ""
    for raw in pieces:
        if _TOKYO23.fullmatch(raw):
            for ward in TOKYO_23:
                items.append({"part": "東京都" + ward, "pref": "東京都", "unit": ward, "note": ""})
            pref, city_prefix = "東京都", ""
            continue
        note = ""
        m = _PREF.match(raw)
        if m:
            pref = m.group(1)
            rest = raw[len(pref):]
            own_pref = pref
        else:
            rest = raw
            unit = _city(rest)
            own_pref = ""
            if unit.endswith("区") and "市" not in unit and city_prefix:
                raw, own_pref = city_prefix + raw, pref  # 「大阪市北区、中央区」の中央区は大阪市の区
                rest = raw[len(pref):]
            elif unit.endswith("区") and "市" not in unit and pref:
                raw, own_pref = pref + raw, pref  # 「東京都江東区、墨田区」。区は同じ都道府県とみなす
                rest = raw[len(pref):]
            elif unit and pref:
                note = f"「{raw}」には都道府県が付いていないため、都道府県を付けずに探します。同じ名前の市町村が他の都道府県にもあるときは、都道府県から入れてください"
        for key in SUBAREAS:
            if raw != key and len(raw) >= 3 and key.endswith(raw):
                raw = key
                own_pref = own_pref or _PREF.match(key).group(1)
                pref = pref or own_pref
                rest = raw[len(own_pref):]
        unit = _city(rest)
        if not unit:
            note = f"「{raw}」は市区町村まで入っていないため、Googleが選んだ周辺の店だけが出ます。都道府県全体・全国を探したことにはなりません。市区町村まで入れてください"
        city_prefix = (own_pref + unit.split("市")[0] + "市") if ("市" in unit and unit.endswith("区")) else ""
        if all(it["part"] != raw for it in items):
            items.append({"part": raw, "pref": own_pref, "unit": unit, "note": note})
    return items or [{"part": norm, "pref": "", "unit": "", "note": ""}]


def area_parts(area):
    """入力の地名を、検索できる1地域ずつに直す(検索語は入力の字のまま。ヶ/ケはそろえない)"""
    return [it["part"] for it in _parse(area)]


def area_scopes(area):
    """エリア外の判定に使う (都道府県, 市区町村) の一覧。市区町村が無い指定(都道府県だけ・全国)は入れない(=判定しない)"""
    out = []
    for it in _parse(area):
        if it["unit"] and (it["pref"], fold(it["unit"])) not in out:
            out.append((it["pref"], fold(it["unit"])))
    return out


def area_units(area):
    """エリア外の判定に使う市区町村名(ヶ/ケをそろえた形)。市区町村が無い指定は空"""
    return list(dict.fromkeys(u for _p, u in area_scopes(area)))


def notes(area):
    """入力の地名について、利用者に伝える注意(探せる範囲が狭い・都道府県を付けなかった、など)"""
    return [it["note"] for it in _parse(area) if it["note"]]


def address_in(address, scopes):
    """住所が、指定の (都道府県, 市区町村) のどれかに入るか。住所に都道府県があれば、それも一致を求める"""
    a = fold(address)
    m = _PREF_IN.search(a)
    apref = m.group(1) if m else ""
    return any(unit in a and (not pref or not apref or pref == apref) for pref, unit in scopes)


def split(area):
    """検索に使う地名の一覧。分割できない地域は、そのまま(正規化した形で)返す。複数の地域は、それぞれを分けて並べる。"""
    out = []
    for part in area_parts(area):
        names = SUBAREAS.get(part)
        out.extend([part + n for n in names] if names else [part])
    return out
