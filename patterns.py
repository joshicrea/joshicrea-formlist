"""業種別メール文面13パターン(利用者が用意する・文面/パターン/)と、17業種からの割り当て。実行環境: Windows / Mac / Linux
割り当てが無い業種は None を返し、呼び出し側が標準の文面にして人が事前確認する(勝手に近いパターンへ寄せない)。"""
import re
from pathlib import Path

import settings_dir

PATTERN_DIR = settings_dir.root() / "文面" / "パターン"  # 利用者ごとの設定(宛先の会社名・署名が入るため、公開物には含めない)。置き場は settings_dir が決める

# 17業種 → パターン番号。明らかに当てはまるものだけ。飲食・医療・レジャー・生活サービス・農業食品は割り当てなし(標準+人が事前確認)
INDUSTRY_TO_PATTERN = {
    "商業施設・百貨店": 1,
    "小売・ショップ": 2,
    "美容・サロン": 2,
    "自動車・乗り物": 3,
    "住宅・不動産": 4,
    "金融・保険・士業": 5,
    "宿泊・観光": 7,
    "ブライダル・婚活": 8,
    "製造・メーカー": 9,
    "教育・学習": 11,  # 2026-11-01 運用者の指示: 旧パターン10(自治体・教育機関・福祉施設)を、10=自治体・地域団体 / 11=教育機関 / 12=福祉施設の3つに分けた
    "福祉・介護": 12,
    "自治体・公共・団体": 10,
}
SPLIT_FROM_10 = (10, 11, 12)  # 旧パターン10から分かれた3つ。11・12 の文面ファイルが無いうちは、旧パターン10の文面を呼び名つきで使う(今までの動きを変えない)

# 業種別の件名の初期値(12パターン・2026-11-01 運用者の指示)。利用者が 文面/パターン/件名.json で業種ごとに変えられる(キーはパターン番号。空・無いキーは初期値)
DEFAULT_SUBJECTS = {
    1: "来館促進や顧客交流につながるハンドメイド体験について",
    2: "ブランドの魅力発信や購入者特典に活かせるハンドメイド企画について",
    3: "来店時の待ち時間や顧客交流に活かせるハンドメイド体験について",
    4: "来場促進やご家族との思い出づくりにつながるハンドメイド体験について",
    5: "お客様との関係づくりや感謝イベントに活かせるハンドメイド体験について",
    6: "来店促進や購入者向けイベントに活かせるハンドメイド体験について",
    7: "宿泊・観光の魅力を高めるハンドメイド体験や記念品制作について",
    8: "ブライダルフェアや記念日に活かせるハンドメイド体験・ギフト制作について",
    9: "社員交流や福利厚生に活かせるハンドメイド体験について",
    10: "地域交流や多世代交流につながるハンドメイド体験について",
    11: "子どもたちの創造力を育むハンドメイド体験・体験学習について",
    12: "施設利用者様の楽しみや交流につながるハンドメイド体験について",
}
SUBJECT_FILE = "件名.json"
# パターン6(家電量販店)とパターン13(フォトスタジオ・写真館)は17業種のどれにも対応しない。業種を問わず、個別に指定したときだけ使う。
EXPLICIT_ONLY = (6, 13)

# パターン10だけ、送り先に合わせて「貴団体」の呼び方を変える
CALLNAME_10 = {"自治体・公共・団体": "貴自治体", "教育・学習": "貴校", "福祉・介護": "貴施設"}


def pattern_for(industry):
    return INDUSTRY_TO_PATTERN.get(industry)


def load_pattern(number, industry="", directory=None):
    d = Path(directory) if directory else PATTERN_DIR
    files = sorted(d.glob("パターン%02d_*.txt" % number))
    if len(files) != 1 and number in SPLIT_FROM_10 and number != 10:
        files = sorted(d.glob("パターン10_*.txt"))  # 11・12 の文面が未作成なら、分割前のパターン10を使う
    if len(files) != 1:
        return None
    text = files[0].read_text(encoding="utf-8-sig")
    if number in SPLIT_FROM_10:
        text = text.replace("貴団体", CALLNAME_10.get(industry, "貴団体"))
    return text


def text_for_industry(industry, directory=None):
    """業種に割り当てたパターンの文面。割り当てが無ければ None"""
    n = pattern_for(industry)
    return load_pattern(n, industry, directory) if n else None


def subject_for_pattern(number, directory=None):
    """パターン番号の件名。利用者の 件名.json(例 {"11": "..."})に空でない値があればそれ、無ければ初期値。番号が表に無ければ None"""
    d = Path(directory) if directory else PATTERN_DIR
    try:
        import json
        over = json.loads((d / SUBJECT_FILE).read_text(encoding="utf-8-sig"))
        v = over.get(str(number)) if isinstance(over, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    except (OSError, ValueError):
        pass
    return DEFAULT_SUBJECTS.get(number)


def set_subject(number, text, directory=None):
    """パターン番号の件名を 件名.json に保存する(空なら上書きを消して初期値に戻す)。他のパターンの件名は残す"""
    import json
    d = Path(directory) if directory else PATTERN_DIR
    d.mkdir(parents=True, exist_ok=True)
    try:
        over = json.loads((d / SUBJECT_FILE).read_text(encoding="utf-8-sig"))
        over = over if isinstance(over, dict) else {}
    except (OSError, ValueError):
        over = {}
    if str(text or "").strip():
        over[str(number)] = str(text).strip()
    else:
        over.pop(str(number), None)
    (d / SUBJECT_FILE).write_text(json.dumps(over, ensure_ascii=False, indent=1), encoding="utf-8")


def subject_for_industry(industry, directory=None):
    """業種に割り当てたパターンの件名。割り当てが無ければ None(呼び出し側が送り主設定の件名を使う)"""
    n = pattern_for(industry)
    return subject_for_pattern(n, directory) if n else None

# パターン番号ごとの名前(ファイル名の後半)。文面テンプレートの追加・状態の表示に使う
PATTERN_NAMES = {
    1: "商業施設・ショッピングモール・百貨店",
    2: "アパレル・コスメ・美容・生活雑貨",
    3: "自動車販売店・カーディーラー",
    4: "住宅メーカー・工務店・不動産会社",
    5: "保険会社・保険代理店",
    6: "家電量販店",
    7: "ホテル・旅館・観光施設・旅行会社",
    8: "ブライダル・結婚式場・ウェディング関連",
    9: "一般企業・人事・総務・福利厚生担当",
    10: "自治体・教育機関・福祉施設・地域団体",
    11: "教育・学習",
    12: "福祉・介護",
    13: "フォトスタジオ・写真館",
}


def pattern_file(number, directory=None):
    """パターン番号の専用ファイル(パターンNN_名前.txt)。ちょうど1つあるときだけ、その Path。無ければ None"""
    d = Path(directory) if directory else PATTERN_DIR
    files = sorted(d.glob("パターン%02d_*.txt" % number)) if d.exists() else []
    return files[0] if len(files) == 1 else None


def save_pattern(number, text, directory=None):
    """パターン番号の文面をファイルに保存する。すでに同じ番号のファイルがあれば、そのファイルへ書く(2つにしない)。
    新規は パターンNN_<PATTERN_NAMES の名前>.txt。保存した Path を返す"""
    if number not in PATTERN_NAMES:
        raise ValueError("パターン番号は1から13です: %r" % (number,))
    d = Path(directory) if directory else PATTERN_DIR
    d.mkdir(parents=True, exist_ok=True)
    target = pattern_file(number, d) or (d / ("パターン%02d_%s.txt" % (number, PATTERN_NAMES[number])))
    target.write_text(str(text), encoding="utf-8")
    return target


def pattern_status(directory=None):
    """パターン番号ごとの状態。present=専用ファイルがあるか / effective=実際に文面が読めるか(11・12は旧パターン10の代用でも可) / fallback=代用で読めているか"""
    d = Path(directory) if directory else PATTERN_DIR
    out = {}
    for n, name in PATTERN_NAMES.items():
        f = pattern_file(n, d)
        effective = load_pattern(n, "", d) is not None
        out[n] = {"name": name, "file": f.name if f else None, "present": f is not None,
                  "effective": effective, "fallback": effective and f is None}
    return out


def patterns_missing(directory=None):
    """文面が読めない(欠けている)パターン番号の一覧"""
    return [n for n, s in pattern_status(directory).items() if not s["effective"]]

