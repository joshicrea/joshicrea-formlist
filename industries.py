"""業種の定義(画面の選択肢・地図の検索語・OpenStreetMapの条件・台帳のメール型)を、ここ1か所で持つ。実行環境: Windows / Python 3.9+。
業種は「広い分類」にする。個別の業態(携帯ショップなど)は選択肢に置かず、各分類が地図の検索語を複数持って取りこぼしを防ぐ。
ここに無い業態は、画面の「キーワード」に語として入れる。
2026-10-26: 業種のチョイスの偏りと細かすぎる分類を直し、抽象度を上げて網羅できる分類にする。検証結果: 90_作業メモ/業種検索語の検証/
"""

# 名前 -> {"map": Googleマップで引く語, "osm": Overpassの条件, "mail": 台帳のメール型}
INDUSTRIES = {
    "飲食・カフェ": {"map": ["飲食店", "カフェ", "レストラン"], "osm": ['nwr["amenity"~"^(restaurant|cafe)$"]'], "mail": "A12"},
    "小売・ショップ": {"map": ["雑貨店", "ブティック", "ギフトショップ"], "osm": ['nwr["shop"~"^(gift|variety_store|interior_decoration|houseware|florist|craft|clothes|boutique)$"]'], "mail": "A6"},
    "美容・サロン": {"map": ["美容室", "ネイルサロン", "エステサロン"], "osm": ['nwr["shop"~"^(hairdresser|beauty|cosmetics)$"]', 'nwr["beauty"]'], "mail": "A6"},
    "医療・健康": {"map": ["クリニック", "整体院", "薬局"], "osm": ['nwr["amenity"~"^(clinic|doctors|dentist|pharmacy)$"]'], "mail": "A12"},
    "宿泊・観光": {"map": ["ホテル", "旅館", "観光施設"], "osm": ['nwr["tourism"~"^(hotel|guest_house|motel|hostel|attraction|museum|theme_park|zoo|aquarium|gallery)$"]', 'nwr["name"~"^道の駅"]'], "mail": "A2"},
    "住宅・不動産": {"map": ["住宅展示場", "不動産", "工務店"], "osm": ['nwr["name"~"住宅展示場|ハウジングセンター|ハウジングステージ|モデルハウス|工務店"]', 'nwr["office"="estate_agent"]', 'nwr["shop"="estate_agent"]'], "mail": "A3"},
    "ブライダル・婚活": {"map": ["結婚式場", "結婚相談所"], "osm": ['nwr["shop"="wedding"]', 'nwr["name"~"結婚式場|ウェディング|ウエディング|ブライダル|迎賓館|結婚相談所|婚活"]'], "mail": "A4"},
    "商業施設・百貨店": {"map": ["ショッピングモール", "百貨店", "ショッピングセンター"], "osm": ['nwr["shop"~"^(mall|department_store)$"]', 'nwr["name"~"ショッピングセンター|ショッピングモール|モール$"]["shop"]'], "mail": "A1"},
    "教育・学習": {"map": ["学校", "学習塾", "カルチャースクール"], "osm": ['nwr["amenity"~"^(college|university|school|kindergarten|language_school|music_school)$"]'], "mail": "A10"},
    "福祉・介護": {"map": ["介護施設", "デイサービス", "福祉施設"], "osm": ['nwr["amenity"="social_facility"]'], "mail": "A10"},
    "自治体・公共・団体": {"map": ["市役所", "公民館", "商工会議所"], "osm": ['nwr["amenity"~"^(townhall|community_centre)$"]'], "mail": "A11"},
    "自動車・乗り物": {"map": ["自動車販売店", "自動車整備", "ガソリンスタンド"], "osm": ['nwr["shop"~"^(car|car_repair|motorcycle)$"]'], "mail": "A8"},
    "金融・保険・士業": {"map": ["保険代理店", "税理士事務所", "銀行"], "osm": ['nwr["office"~"^(insurance|accountant|lawyer|tax_advisor)$"]', 'nwr["amenity"="bank"]'], "mail": "A8"},
    "製造・メーカー": {"map": ["メーカー", "製造", "工場"], "osm": ['nwr["man_made"="works"]', 'nwr["office"="company"]["industrial"]'], "mail": "A6"},
    "レジャー・スポーツ": {"map": ["フィットネスクラブ", "スポーツクラブ", "アミューズメント施設"], "osm": ['nwr["leisure"~"^(fitness_centre|sports_centre|bowling_alley|amusement_arcade)$"]'], "mail": "A12"},
    "生活サービス": {"map": ["クリーニング店", "ペットサロン", "リフォーム店"], "osm": ['nwr["shop"~"^(dry_cleaning|laundry|pet|repair)$"]'], "mail": "A12"},
    "農業・食品・直売": {"map": ["直売所", "農園", "食品店"], "osm": ['nwr["shop"~"^(farm|greengrocer|bakery|confectionery)$"]', 'nwr["amenity"="marketplace"]', 'nwr["name"~"直売所|産直"]'], "mail": "A7"},
}

# 以前の業種名 -> いまの業種名。古い指示・古いリストの業種名で呼ばれても、新しい分類で探せるようにする
LEGACY = {
    "ホテル・旅館": "宿泊・観光", "観光施設": "宿泊・観光",
    "百貨店": "商業施設・百貨店", "商業施設": "商業施設・百貨店",
    "ブライダル": "ブライダル・婚活", "結婚相談所": "ブライダル・婚活",
    "自動車・保険": "自動車・乗り物", "携帯ショップ": "小売・ショップ",
    "美容・コスメ": "美容・サロン", "サロン": "美容・サロン",
    "アパレル": "小売・ショップ", "ライフスタイル雑貨": "小売・ショップ",
    "食品・生産者直売": "農業・食品・直売", "教育機関": "教育・学習", "福祉施設": "福祉・介護",
    "自治体・地域団体": "自治体・公共・団体", "メーカー": "製造・メーカー",
}

NAMES = list(INDUSTRIES)


def canonical(name):
    """古い業種名を新しい名前にする。新しい名前・一覧に無い名前はそのまま返す"""
    return LEGACY.get(name, name)
