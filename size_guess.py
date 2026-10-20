"""会社の規模の目安(推定)。大手・チェーンは、問い合わせフォームが本社の窓口で、担当者に届きにくい。
判定は「地図の登録情報」「会社名」「同じ検索結果に同じHPの店が複数ある」の3つだけで、売上や従業員数は見ていない。
対照: 大手に見えないのに大手の会社は取りこぼす(個別のホテルが運営会社だけ大手、など)。外れたら「送らない」で外す"""
import re
from collections import Counter

import ng_list

# 全国チェーン・大手運営会社の名前に出る語(部分一致)。増やすときは実在するものだけ入れる
BIG_NAMES = re.compile(
    "ヒルトン|ハイアット|マリオット|ウェスティン|シェラトン|ヒルトン|インターコンチネンタル|ホリデイ・イン|ハイアット|リッツ|ウォルドーフ|コンラッド|"
    "アパホテル|東横イン|東横INN|ルートイン|ドーミーイン|スーパーホテル|ホテルマイステイズ|相鉄フレッサ|サンルート|京王プラザ|"
    "プリンスホテル|ニューオータニ|帝国ホテル|オークラ|西鉄イン|ソラリア|ワシントンホテル|ホテルJALシティ|JAL ?シティ|ANAクラウンプラザ|"
    "三井ガーデン|三井不動産|三菱地所|東急|西武|阪急|阪神|近鉄|京急|小田急|JR|イオン|ららぽーと|ルミネ|アトレ|パルコ|PARCO|マルイ|"
    "高島屋|三越|伊勢丹|大丸|松坂屋|そごう|丸井|ダイソー|ユニクロ|無印良品|ニトリ|ヨドバシ|ビックカメラ|ヤマダ|"
    "QBハウス|EARTH|AFLOAT|TBC|ミュゼ|キレイモ|脱毛ラボ|ラ・ヴェリテ|ドクターシーラボ|LAVA|カーブス|エニタイム|ホットペッパー|"
    "ドコモ|ソフトバンク|au |ａｕ|積水ハウス|大和ハウス|住友林業|旭化成ホームズ|ミサワホーム|パナソニック ホームズ|セキスイ|"
    "ゼクシィ|ツルハ|マツモトキヨシ|ウエルシア|サンドラッグ|スギ薬局|ココカラ|しまむら|ワークマン|ライフ |サミット|西友|"
    "ベルーナ|ナチュラルローソン|ファミリーマート|ローソン|セブン.イレブン|スターバックス|マクドナルド|ドトール")
# 名前に含まれると大型施設とみなす語
BIG_FACILITY = re.compile("タワー|グランド|グランヴィア|リゾート&|ラグジュアリー|コンベンション|ショッピングセンター|モール|ステーション|駅ビル|ターミナル|ヒルズ|アウトレット")


def _int(v):
    m = re.match(r"\d+", str(v or "").strip())
    return int(m.group()) if m else 0


def judge(row, tags=None, same_domain=1):
    """('大手・チェーン', 理由) か ('中小(推定)', '') を返す"""
    tags = tags or {}
    name = row.get("name", "")
    if tags.get("brand") or tags.get("brand:wikidata"):
        return "大手・チェーン", "地図にブランド(チェーン名)が登録されている"
    if tags.get("operator:wikidata") or tags.get("wikidata"):
        return "大手・チェーン", "地図で著名な施設・運営会社として登録されている"
    if _int(tags.get("rooms")) >= 150:
        return "大手・チェーン", f"客室{_int(tags.get('rooms'))}室"
    if _int(tags.get("stars")) >= 4:
        return "大手・チェーン", f"星{_int(tags.get('stars'))}つ"
    if _int(tags.get("building:levels")) >= 10:
        return "大手・チェーン", f"{_int(tags.get('building:levels'))}階建て"
    m = BIG_NAMES.search(name)
    if m:
        return "大手・チェーン", f"全国チェーン・大手の名前（{m.group().strip()}）"
    m = BIG_FACILITY.search(name)
    if m:
        return "大手・チェーン", f"大型施設の名前（{m.group()}）"
    if same_domain >= 3:
        return "大手・チェーン", f"同じホームページの店・施設が{same_domain}件ある"
    return "中小(推定)", ""


def mark(rows):
    """rows(取得した会社の辞書のリスト)に size・size_why を付ける。同じ検索結果で同じドメインが3件以上ならチェーンとみなす"""
    count = Counter(ng_list.domain(r["url"]) for r in rows if r.get("url"))
    for r in rows:
        d = ng_list.domain(r["url"]) if r.get("url") else ""
        r["size"], r["size_why"] = judge(r, r.pop("tags", None), count.get(d, 1) if d else 1)
    return rows
