"""会社の方針・思いを、ホームページの本文から拾い、書き出しの根拠にする。書き出しは2文(第1文=相手の特徴の言い切り、第2文=送る文面の提案への接続)(2026-11-02 運用者の指示「会社の方針思いを拾って文章つくるように」)。
拾うのはトップページの見出し・段落と、理念・コンセプト・こだわり等のリンク先(同じホストの最大2ページ)。
書き出しは根拠の文の引用だけで作る(言い換えて意味を変えない・書いていないことを足さない)。拾えない会社は空のまま、理由を言う。
実行環境: Windows / Linux / macOS (純粋な文字列処理。取得は site_checker.Site を呼ぶ側が渡す)"""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# 方針・思いの語(強い順に重み)。これを含む文を最優先で拾う
THOUGHT = re.compile(r"理念|想い|思い(?!切|出|込|違|つき|がけ)|信念|モットー|ポリシー|大切に|こだわ|目指|めざ|心がけ|心掛け|寄り添|ミッション|ビジョン|お届け|守り|守る|ひとり.{0,3}ひとり|一人.{0,3}一人|ために|ことを第一|第一に|方針|目標|理想|成長|貢献|使命|価値観|誇り|姿勢|約束|考え方")
# 理念型・こだわり型・特徴型の見分け(決定的な規則)
KIND_RIDEN = re.compile(r"理念|想い|思い(?!切|出|込|違|つき|がけ)|信念|モットー|ポリシー|ミッション|ビジョン|目指|めざ|理想|使命|目標|方針|価値観")
KIND_KODAWARI = re.compile(r"こだわ|大切に|心がけ|心掛け|寄り添|第一に|守")

# 候補から外す文(予約・クーポン・料金・営業時間・アクセス・求人・プライバシー・ナビ)
EXCLUDE = re.compile(r"予約|クーポン|キャンペーン|キャンペイン|料金|価格|円|税込|営業時間|定休|アクセス|求人|採用|スタッフ募集|プライバシー|個人情報|"
                     r"お問い?合わ?せ|TEL|Tel|tel|電話|〒|住所|ログイン|カート|Copyright|©|メニュー|ご利用ください|詳しくはこちら|"
                     r"クリック|タップ|ホットペッパー|HOT PEPPER|ポイント|割引|OFF|%|％|SALE|セール|ブログ更新|お知らせ|NEWS|news|￥|¥|感染|営業再開|通常営業|短縮営業|休業|緊急事態|コロナ|ウイルス|消毒|購入|通販|ショップ|ストア|販売|送料|在庫|人材|求めています|一緒に働|仲間を|◎|★|☆|♪|♥|❤|人気|おすすめ|オススメ|口コミ|評判|ランキング", re.I)
# どの会社にも当てはまる文(減点。単独で根拠にしない)
GENERIC = re.compile(r"素敵な|ようこそ|ご覧いただき|ありがとうございます|お待ちして|心よりお待ち|皆様のご来店|お気軽に|ぜひ|是非|当店のホームページ|当サイト|このサイト")
# 具体名詞とみなさない語(会社固有ではない、どの会社の文にも出る語)
STOP_NOUN = {"お客様", "お客さま", "私たち", "わたし", "当店", "当社", "当院", "皆様", "みなさま", "スタッフ", "サービス", "ホームページ", "気持ち",
             "ひとり", "一人", "ご来店", "毎日", "笑顔", "全力", "大切", "想い", "思い", "こだわり", "理念", "お届け", "お手伝い", "安心", "提供",
             "ありがとう", "感謝", "心地", "時間", "空間", "技術", "最高", "最大", "最新", "ひとりひとり", "一人一人", "これから", "いつも"}
NOUN = re.compile(r"[一-龥]{2,}|[ァ-ヶー]{3,}")

PAGE_LINK = re.compile(r"理念|コンセプト|concept|想い|思い|こだわり|特徴|強み|私たちについて|わたしたちについて|代表|メッセージ|ご挨拶|挨拶|about|philosophy|message|feature|policy|vision|mission|story|ストーリー|大切にしてい", re.I)
SKIP_LINK = re.compile(r"予約|reserv|booking|member|login|password|regist|会員登録|ログイン|求人|採用|recruit|privacy|faq|よくある|blog|news|topics|sitemap|policy.*privacy", re.I)

MIN_SCORE = 5
CONCEPT_BONUS = 2  # 理念・コンセプト等のページから拾った文への加点(ページ自体が方針を書く場所のため)
MAX_SENTENCES = 2
QUOTE_MIN, QUOTE_MAX = 12, 60  # 引用の長さ(書き出し全体で40〜90字に収めるため)
END_OK = re.compile(r"(ます|です|ました|たい|ている|いる|ある|こと|事|もの|ため|サロン|美容室|企業|会社|店|院|空間|場所)$")  # 読点で切った引用の終わりとして不自然でない形


def split_sentences(text):
    """本文を文に分ける。「。」「！」「？」と改行で切る。短すぎる断片・長すぎる塊は捨てる"""
    out = []
    for block in re.split(r"[\r\n]+", text or ""):
        for s in re.split(r"(?<=[。！？!?])", block):
            s = re.sub(r"\s+", " ", s).strip(" 　・|｜>＞»")
            if 12 <= len(s) <= 140:
                out.append(s)
    return out


def specific_nouns(s):
    """文に含まれる会社固有の語(一般語を除く)。書き出しが「どの会社にも言える文」になっていないかの機械条件に使う"""
    found = []
    for m in NOUN.findall(s):
        if m in STOP_NOUN or any(m == w for w in STOP_NOUN):
            continue
        if re.fullmatch(r"[一-龥]{2}", m) and m in {"自分", "自身", "最初", "最後", "今日", "本当", "全て", "皆様", "場所", "場合", "必要", "可能", "気軽", "安心", "快適"}:
            continue
        found.append(m)
    return found


def score(s):
    """文の点数。方針・思いの語が主、会社固有の具体語が従。除外語を含む文・汎用文は低くする"""
    if EXCLUDE.search(s):
        return -99
    n = 0
    hits = THOUGHT.findall(s)
    if hits:
        n += 3 + min(2, len(hits) - 1)
    n += min(3, len(specific_nouns(s)))
    if GENERIC.search(s):
        n -= 3
    if not re.search(r"[ぁ-ん]", s):
        n -= 3  # 名詞の羅列(メニュー・ナビ)
    if not re.search(r"[。！？!?]$", s):
        n -= 3  # 文の終わりが無い断片(見出し・途切れた行)は、引用すると文が欠ける
    return n


def page_sentences(soup):
    """ページの見出しと本文の段落から文を作る(ナビ・フッター・スクリプトは見ない)"""
    for t in soup(["script", "style", "nav", "footer", "header", "noscript", "form", "select", "option"]):
        t.decompose()
    parts = []
    for t in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "dd", "blockquote"]):
        txt = re.sub(r"\s+", " ", t.get_text(" ", strip=True))  # 要素の中の改行跡で文が割れないようにする
        if txt:
            parts.append(txt)
    return split_sentences("\n".join(parts))


def link_candidates(soup, final, limit=2):
    """理念・コンセプト・こだわり等のリンク先(同じホスト)。最大 limit 件"""
    host = urlparse(final).netloc
    seen, out = {final.split("#")[0]}, []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        label = a.get_text(" ", strip=True) + " " + href
        if not PAGE_LINK.search(label) or SKIP_LINK.search(label):
            continue
        u = urljoin(final, href).split("#")[0]
        if urlparse(u).netloc != host or u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            break
    return out


def pick(pages):
    """pages = [(url, sentences, concept)]。concept=理念・コンセプト等のページ。点数の高い順に最大2文。(文, 取得元URL, 点数) のリスト。
    MIN_SCORE 未満は拾わない。トップなど理念ページ以外では、方針・思いの語を含む文だけを拾う(商品説明・宣伝文を根拠にしない)"""
    cands = []
    for url, sents, concept in pages:
        for s in sents:
            sc = score(s)
            if sc < 0:
                continue
            if not THOUGHT.search(s):
                continue  # 方針・思いの語が無い文は、理念ページの文でも根拠にしない(所在地・実績・宣伝文を引かない)
            if concept:
                sc += CONCEPT_BONUS
            if sc >= MIN_SCORE:
                cands.append((sc, s, url))
    cands.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for sc, s, url in cands:
        if s in seen:
            continue
        seen.add(s)
        out.append((s, url, sc))
        if len(out) >= MAX_SENTENCES:
            break
    return out


def collect(soup, final, site):
    """トップ本文とリンク先から根拠文を拾う。戻り値: {"basis": "文1 / 文2", "url": "取得元URL(複数は改行でなく ' ' 区切り)", "why": 空なら拾えなかった理由}。
    取得に使う site は site_checker.Site(robots・転送先確認・取得予算つき)。HttpBudgetExceeded などはそのまま上へ投げる"""
    top = page_sentences(BeautifulSoup(str(soup), "html.parser"))
    pages = [(final, top, False)]
    for u in link_candidates(soup, final):
        f, h = site.get(u, url_allowed=lambda x, _host=urlparse(final).netloc: urlparse(x).netloc == _host)
        if f is None:
            continue
        pages.append((f, page_sentences(BeautifulSoup(h, "html.parser")), True))
    got = pick(pages)
    if got:
        return {"basis": " / ".join(s for s, _u, _sc in got), "url": " ".join(dict.fromkeys(u for _s, u, _sc in got)), "why": ""}
    total = sum(len(p[1]) for p in pages)
    if total == 0:
        why = "ホームページの本文が読めなかった(画面の描画に頼るページの可能性)"
    elif len(pages) == 1:
        why = "トップページに方針・思いを書いた文が見つからず、理念・こだわりのページへのリンクも無かった"
    else:
        why = f"トップと理念・こだわり等のページ{len(pages) - 1}件を読んだが、方針・思いを書いた文が見つからなかった"
    return {"basis": "", "url": "", "why": why}



# ---- v3(2026-11-02 運用者の指摘「一行目と2行目が繋がっておらず浮いている、誰目線?」で v2 を差し替え) ----
# 目線は送り手(文面の差出人)。第1文=お客様・過ごす時間・場面を前提にした「ご提案したく、ご連絡しました」。第2文=その場面に合う提案物(送る文面にある語だけ)。
# 会社を評価する独立文は置かない。提案につながらない特徴(薬剤へのこだわり等)は根拠にしない。場面が拾えない会社は空欄(社数が減ってよい)
BANNED = re.compile(r"拝見|共感|惹か|素敵|魅力的|素晴らしい|感銘|感動|敬服|感服|ご立派|ぜひご覧|ご覧ください")
QUOTE_MARKS = re.compile(r"[『』「」]")
NEGATED = re.compile(r"ません|ていない|いない|ない|なかっ|なく|ず|たら|れば")  # 根拠の語の直後の否定・条件。場面の根拠にしない
# 第1文が会社を評価して終わる述語(v2 の浮いた形)。これで終わる第1文は不合格
BAD_FIRST_END = re.compile(r"(?:大切に(?:され|し)|目指し|こだわっ|力を入れ|心がけ|心掛け)て(?:い|おられ)ます。$")
SENDER_ACT = re.compile(r"ご提案|ご用意|ご連絡")
# 提案の具体語(その文面に実際にある語だけを使う)
PROPOSAL_TERMS = ("制作体験", "ワークショップ", "体験", "イベント", "ギフト", "記念品", "特典")

# 提案物の種類 → その場面に合う、送る文面の提案語(この語だけを使い、他の種類の語に流れない)
KIND_TERMS = {"long": ("制作体験", "体験"), "workshop": ("ワークショップ", "イベント"), "gift": ("ギフト", "記念品", "特典")}
KIND_LABEL = {"long": "滞在が長い→制作体験", "workshop": "交流の場→ワークショップ", "gift": "滞在が短い・記念→ギフト"}
def _s(text):
    return lambda kw: text


# 施術・ケアの語ごとに (第1文の前提, 第1文の目的, 第2文の導入)。語ごとに目的も導入も変え、「語が1つ違うだけの同じ文」にしない
_CARE = {
    "ヘッドスパ": ("ヘッドスパを受けに来られるお客様へ", "頭皮と髪を整えるひとときを楽しんでいただく企画", "ヘッドスパの前後のお時間に、"),
    "頭皮": ("頭皮ケアの施術を受けに来られるお客様へ", "頭皮と髪を整えるひとときを楽しんでいただく企画", "施術の前後のお時間に、"),
    "髪質改善": ("髪質改善の施術にお時間をかけて来られるお客様へ", "仕上がりを待つ時間を楽しんでいただく企画", "仕上がりをお待ちの時間に、"),
    "トリートメント": ("トリートメントの施術を受けに来られるお客様へ", "施術の待ち時間を楽しんでいただく企画", "施術の待ち時間に、"),
    "ケア": ("ケアの施術を受けに来られるお客様へ", "施術のお時間を楽しんでいただく企画", "施術の合間に、"),
}
_RELAX = {"癒し": "癒しの時間", "くつろ": "くつろぎの時間", "リラックス": "くつろぎの時間", "ゆっくり": "ゆっくり過ごす時間", "心地": "心地よい時間", "快適な時間": "快適な時間"}
# (場面名, 根拠の語, 第1文の前提(お客様・時間・場面)を作る関数, 第1文の目的を作る関数, 提案物の種類, 第2文の導入を作る関数)。上から順に、最初に作れた場面を使う
SCENES = [
    ("家族連れ", re.compile(r"家族|ご友人|親子|お子"), _s("ご家族やご友人と過ごされるお客様へ"), _s("お客様同士で楽しんでいただく企画"), "workshop", _s("ご家族やご友人と一緒に楽しめる場として、")),
    ("気軽な来店", re.compile(r"気軽に(?:お)?立ち寄"), _s("気軽に立ち寄られるお客様へ"), _s("お立ち寄りのひとときを楽しんでいただく企画"), "long", _s("お立ち寄りの際の待ち時間に、")),
    ("施術中の過ごし方", re.compile(r"施術中"), _s("施術中の時間をお過ごしのお客様へ"), _s("施術中のお時間を楽しんでいただく企画"), "long", _s("施術中のお時間に、")),
    ("癒しの時間", re.compile(r"癒し|くつろ|リラックス|ゆっくり|心地|快適な時間"), lambda kw: _RELAX[kw] + "を求めて来店されるお客様へ", _s("その時間をさらに楽しんでいただく企画"), "long", _s("ゆったりお過ごしのお時間に、")),
    ("ケアの施術", re.compile("|".join(_CARE)), lambda kw: _CARE[kw][0], lambda kw: _CARE[kw][1], "long", lambda kw: _CARE[kw][2]),
    ("短時間の来店", re.compile(r"短い時間|時短|スピーディ|手早"), _s("短い時間でのご来店を求めるお客様へ"), _s("ご来店の記念になるものをお渡しできる企画"), "gift", _s("滞在が短くても無理なく取り入れられる、")),
    ("記念日", re.compile(r"記念日|お祝い|ご褒美|プレゼント|晴れの日"), _s("大切な日にご来店のお客様へ"), _s("ご来店の記念になるものをお渡しできる企画"), "gift", _s("お帰りの際にお渡しできる、")),
    ("地域のお客様", re.compile(r"地域|近隣"), _s("地域のお客様が通われる御社へ"), _s("地域の方との接点づくりになる企画"), "workshop", _s("地域の方とお客様が一緒に楽しめる場として、")),
    ("お客様との交流", re.compile(r"つながり|交流|コミュニティ"), _s("お客様とのつながりを大切にする御社へ"), _s("お客様同士やスタッフとの交流が生まれる企画"), "workshop", _s("会話が生まれる場として、")),
]


def proposal_phrases(template, kind_terms, strict=False):
    """送る文面(template)に実際にある提案の具体語を、kind_terms の順(strict でなければ続けて全提案語)に並べた [(語, 提案の句)]。同じ句は1つ。無ければ []"""
    text = template or ""
    base = "本物のお花を使った" if ("本物のお花" in text or "本物の花" in text) else ""
    out, seen, spans = [], set(), []
    for term in (tuple(kind_terms) if strict else tuple(kind_terms) + PROPOSAL_TERMS):
        m = re.search(r"(?:(?<![一-龥ぁ-んァ-ヶー])(?!企業向けの)[一-龥ぁ-んァ-ヶー]{1,4}の)?" + re.escape(term) + ("会?" if term == "体験" else ""), text)  # 「企業向けの」は相手の会社を指す語で、書き出しでは不自然なので前置きに取らない
        if not m or (base + m.group(0)) in seen:
            continue
        pos = text.find(term, m.start())  # 語そのものの位置。別の語の一部(制作体験の「体験」)なら、同じ提案の言い換えなので候補にしない
        if any(a <= pos < b for a, b in spans):
            continue
        spans.append((m.start(), m.end()))
        seen.add(base + m.group(0))
        out.append((term, base + m.group(0)))
    return out


def first_sentence(opener):
    return (opener or "").split("。")[0] + "。"


def valid_opener(opener, template=""):
    """v3 の合格条件: 2文・第1文に送り手の行為(ご提案/ご用意/ご連絡)・第1文が会社評価の述語で終わらない・禁止語と引用符なし・第2文に文面の提案語"""
    parts = [x for x in (opener or "").split("。") if x]
    if len(parts) != 2 or not SENDER_ACT.search(parts[0]) or BAD_FIRST_END.search(parts[0] + "。"):
        return False
    if BANNED.search(opener) or QUOTE_MARKS.search(opener):
        return False
    return not template or any(p in parts[1] for _t, p in proposal_phrases(template, PROPOSAL_TERMS, strict=True))


def opener_traces(basis, template=""):
    """(書き出し, {根拠, 場面, 提案物}) を候補の順に出す。根拠の語(basis にある)が第1文の前提を決め、その場面の種類に合う提案語(文面にある語だけ)が第2文を決める。
    場面が拾えない・場面に合う提案語が文面に無い会社は何も出さない(汎用の接続で埋めない)"""
    text = basis or ""
    for scene, rx, premise, purpose, kind, when in SCENES:
        for m in rx.finditer(text):
            if NEGATED.search(re.split(r"[。、]", text[m.end():m.end() + 16])[0]):  # 同じ句の中(読点・句点まで)の否定・条件
                continue
            kw = m.group(0)
            first = f"{premise(kw)}、{purpose(kw)}をご提案したく、ご連絡しました。"
            for term, phrase in proposal_phrases(template, KIND_TERMS[kind], strict=True):
                out = f"{first}{when(kw)}{phrase}をご用意できます。"
                if valid_opener(out):
                    yield out, {"根拠": kw, "場面": scene, "提案物": phrase, "種類": KIND_LABEL[kind]}
            break  # 同じ場面の2つ目以降の語は同じ第1文を作りやすいので、場面ごとに最初の語だけ


def opener_variants(basis, template=""):
    for out, _t in opener_traces(basis, template):
        yield out


def first_sentence_form(opener):
    """第1文の「型」: 前提(お客様へ)より後ろの、目的の句まで。形容の語だけが違う第1文は、この型が同じになる(他社の第1文と入れ替えて成立する)"""
    return first_sentence(opener).split("、", 1)[-1]


def opener_from_basis(basis, template="", taken=()):
    """最初の合格候補。taken(他の会社の書き出し)と、2文とも同じもの・第1文が同じもの・第1文の型が同じものは飛ばす(他社の第1文と入れ替えて成立する文を作らない)。無ければ空文字"""
    taken_first = {first_sentence(t) for t in taken if t}
    taken_form = {first_sentence_form(t) for t in taken if t}
    for out in opener_variants(basis, template):
        if out not in taken and first_sentence(out) not in taken_first and first_sentence_form(out) not in taken_form:
            return out
    return ""
