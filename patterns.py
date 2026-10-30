"""業種別メール文面10パターン(利用者が用意する・文面/パターン/)と、17業種からの割り当て。実行環境: Windows / Mac / Linux
割り当てが無い業種は None を返し、呼び出し側が標準の文面にして人が事前確認する(勝手に近いパターンへ寄せない)。"""
import re
from pathlib import Path

PATTERN_DIR = Path(__file__).resolve().parent / "文面" / "パターン"  # 利用者ごとの設定(宛先の会社名・署名が入るため、公開物には含めない)

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
    "教育・学習": 10,
    "福祉・介護": 10,
    "自治体・公共・団体": 10,
}
# パターン6(家電量販店)は17業種のどれにも対応しない。業種を問わず、個別に指定したときだけ使う。
EXPLICIT_ONLY = (6,)

# パターン10だけ、送り先に合わせて「貴団体」の呼び方を変える
CALLNAME_10 = {"自治体・公共・団体": "貴自治体", "教育・学習": "貴校", "福祉・介護": "貴施設"}


def pattern_for(industry):
    return INDUSTRY_TO_PATTERN.get(industry)


def load_pattern(number, industry="", directory=None):
    d = Path(directory) if directory else PATTERN_DIR
    files = sorted(d.glob("パターン%02d_*.txt" % number))
    if len(files) != 1:
        return None
    text = files[0].read_text(encoding="utf-8-sig")
    if number == 10:
        text = text.replace("貴団体", CALLNAME_10.get(industry, "貴団体"))
    return text


def text_for_industry(industry, directory=None):
    """業種に割り当てたパターンの文面。割り当てが無ければ None"""
    n = pattern_for(industry)
    return load_pattern(n, industry, directory) if n else None
