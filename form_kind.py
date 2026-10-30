"""フォームの種別判定(汎用の問い合わせ / 用途限定 / 判定できない)。実行環境: Windows / Mac / Linux(Python 3.9+)
営業文は汎用の問い合わせフォームに送る。入居相談・見学申込・資料請求・採用などの用途限定フォームへ送ると、受け手の本来の用途を妨げ、迷惑になる。
材料は、フォームの項目名・見出し・ボタン文言・URL。判定は1つのフォームごと(会社単位ではない)。
  general: 見出し・ボタン・URL が「お問い合わせ」系で、用途限定の語が見出し・ボタン・URL・項目名のどこにも無い
  purpose: 用途限定の語が見出し・ボタン・URL・項目名のいずれかにある(項目名に出るのは、申込・予約の入力欄の印)。ただし見出しやボタンが「お問い合わせ」系でも、項目に用途限定の欄があれば purpose
  unknown: 上のどちらにも当たらない。または「お問い合わせ」系の語と用途限定の語が見出し・ボタン・URLの中で混ざっている。unknown は general として数えない
見ていないもの: 画像だけの見出し・ボタン、JavaScriptで後から出る項目、項目名の言い換え(PURPOSE_* に無い語)。判定は候補であり、unknown と purpose は人が見る一覧に回す"""
import copy
import re

# 見出し・ボタン・URL・項目名に出ると「用途限定」とみなす語(一般の「相談」「質問」は含めない)
PURPOSE_WORDS = re.compile(
    r"入居|見学|体験|資料請求|資料ダウンロード|カタログ|予約|申込|申し込み|お申込|応募|エントリー|採用|求人|リクルート|出店|出展|取材|寄付|"
    r"介護相談|ケアプラン|入会|会員登録|来店|試乗|見積|キャンセル|診察|受診|"
    r"apply|reserv|booking|entry|recruit|career|catalog|download|trial|visit", re.I)
# 項目名にだけ現れる、申込・予約の入力欄の印(一般の問い合わせでは求めない情報)
PURPOSE_FIELDS = re.compile(
    r"希望日|希望時期|希望時間|ご?希望の?(日|時|職種|プラン|コース)|第[1-3一二三]希望|入居希望|見学希望|要介護|要支援|ご利用者|ご本人様|生年月日|"
    r"資料の?種類|応募職種|履歴書|職務経歴|来店日|ご利用日|宿泊日|人数|診療科", re.I)
# 区分欄の選択肢に「総合・その他・ご意見・ご質問・ご相談」があれば、用途語(入居・見学・求人…)が並んでいても窓口の振り分け。選択肢の語は用途限定の根拠にしない
ROUTER_OPTION = re.compile(r"総合|その他|ご意見|ご質問|お問い?合わ?せ|問合せ|ご相談|全般")
GENERAL_WORDS = re.compile(r"お問い?合わ?せ|問合せ|問い合わせ|contact|inquiry|enquiry|toiawase|otoiawase|お問合わせ", re.I)


def _texts(tag):
    return [t for t in (tag.get_text(" ", strip=True),) if t]


def _texts_without_options(tag):
    """ラベルの文字から、中に入っている選択肢(select/option)の文字を除く。選択肢は _choice_groups で別に扱う"""
    c = copy.copy(tag)
    for x in c.find_all(["select", "option"]):
        x.decompose()
    return _texts(c)


def _choice_groups(form):
    """選択肢の組(select の option 全部/同じ name のラジオ・チェックの文言)を [[文言…]] で返す"""
    groups = []
    for s in form.find_all("select"):
        g = [o.get_text(" ", strip=True) for o in s.find_all("option")]
        groups.append([x for x in g if x])
    by_name = {}
    for i in form.find_all("input"):
        if (i.get("type") or "").lower() not in ("radio", "checkbox"):
            continue
        lab = None
        if i.get("id"):
            lab = form.find("label", attrs={"for": i.get("id")})
        if lab is None:
            lab = i.find_parent("label")
        if lab is not None:
            txt = lab.get_text(" ", strip=True)
            if txt:
                by_name.setdefault(i.get("name") or "", []).append(txt)
    groups += list(by_name.values())
    return groups


def _strip_note(text):
    """項目名の後ろの注記(※… や （…）の案内文)は、項目の種類を表さないので除く。例: 「お問い合わせ内容 ※見学のお申し込みは別のフォームから」"""
    return re.split(r"[※＊*]", text, maxsplit=1)[0].strip()


def _placeholder_word(text):
    """入力欄の薄い例文(「例: 入居を検討しています…」のような文)は項目名ではない。短い語(「希望日」等)だけ項目名として数える"""
    if len(text) > 12 or re.search(r"[。、]|^例|^e\.g", text):
        return ""
    return text


def _is_router(group):
    """2つ以上の選択肢があり、そのどれかが「総合・その他・ご意見…」: 窓口を選ぶだけの区分欄"""
    return len(group) >= 2 and any(ROUTER_OPTION.search(x) and not PURPOSE_WORDS.search(x) for x in group)  # 「入居のご相談」のように用途語を含む選択肢は、振り分けの根拠にしない


def _signals(form, page_url="", page_title=""):
    """(見出し, ボタン, URL, 項目名) の文字列4つ。見出しは、フォーム内の見出しと、直前の見出し(h1〜h3)"""
    heads = []
    for h in form.find_all(["h1", "h2", "h3", "h4", "legend"]):
        heads += _texts(h)
    prev = form.find_previous(["h1", "h2", "h3"])
    if prev is not None:
        heads += _texts(prev)
    if page_title:
        heads.append(page_title)
    buttons = []
    for b in form.find_all("button"):
        buttons += _texts(b)
    for i in form.find_all("input"):
        if (i.get("type") or "").lower() in ("submit", "button", "image"):
            buttons.append(i.get("value") or i.get("alt") or "")
    groups = _choice_groups(form)
    routers = {x for g in groups if _is_router(g) for x in g}
    fields = []
    for g in groups:
        if not _is_router(g):
            fields += g  # 振り分けでない選択肢(希望日の候補など)は、今までどおり項目名として数える
    for lab in form.find_all(["label", "th", "dt"]):
        fields += [_strip_note(x) for x in _texts_without_options(lab) if x not in routers]
    for el in form.find_all(["input", "select", "textarea"]):
        if (el.get("type") or "").lower() in ("submit", "button", "image", "hidden"):
            continue
        fields += [el.get("name") or "", el.get("id") or "", _placeholder_word(el.get("placeholder") or ""), el.get("aria-label") or ""]
    action = form.get("action") or ""
    return " ".join(heads), " ".join(buttons), f"{page_url} {action}", " ".join(fields)


def classify_form(form, page_url="", page_title=""):
    """BeautifulSoup の form 要素1つを判定する。{"kind": "general"|"purpose"|"unknown", "reasons": [理由…]}。理由は人が見る一覧に出す"""
    head, button, url, fields = _signals(form, page_url, page_title)
    top = " ".join([head, button, url])  # 見出し・ボタン・URL
    p_top = sorted({m.group(0).lower() for m in PURPOSE_WORDS.finditer(top)})
    p_field = sorted({m.group(0) for m in PURPOSE_FIELDS.finditer(fields)} | {m.group(0).lower() for m in PURPOSE_WORDS.finditer(fields)})
    g_top = bool(GENERAL_WORDS.search(top))
    if p_field:  # 見出しが「お問い合わせ」系でも、項目に用途限定の欄があれば申込・予約のフォームとして扱う
        return {"kind": "purpose", "reasons": ["項目に用途限定の欄: " + "・".join(p_field[:4])] + (["見出し・ボタン・URLにも: " + "・".join(p_top[:3])] if p_top else [])}
    if p_top and g_top:
        return {"kind": "unknown", "reasons": ["「お問い合わせ」系と用途限定の語が混ざっている: " + "・".join(p_top[:3])]}
    if p_top:
        return {"kind": "purpose", "reasons": ["見出し・ボタン・URLに用途限定の語: " + "・".join(p_top[:3])]}
    if g_top:
        return {"kind": "general", "reasons": ["見出し・ボタン・URLが「お問い合わせ」系で、用途限定の語が無い"]}
    return {"kind": "unknown", "reasons": ["見出し・ボタン・URL・項目名のどこにも、問い合わせか用途限定かを分ける語が無い"]}


def prefer_general(forms, page_title=""):
    """[(form, url)] から (汎用, 判定できない, 用途限定) の3つの [(form, url, 判定)] を返す。用途限定は別に返す(除外して別表示するため)"""
    general, unknown, purpose = [], [], []
    for form, url in forms:
        r = classify_form(form, url, page_title)
        {"general": general, "unknown": unknown, "purpose": purpose}[r["kind"]].append((form, url, r))
    return general, unknown, purpose
