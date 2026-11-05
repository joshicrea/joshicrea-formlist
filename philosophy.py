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



# ---- v4(2026-11-02 運用者の訂正で v3 を差し替え) ----
# 提案は「施術の時間に足す楽しみ」ではなく、新規を集める・リピーターを作る・接触回数を増やす施策。ワークショップ/制作体験の開催はその手段。
# 目線は送り手(文面の差出人)。第1文=その会社の事実を前提にした施策の提案、第2文=開催が3目的のどれにどう効くかの言い切り。
# 施術中・前後・待ち時間・滞在時間を舞台にした文は作らない。前提から施策がつながらない会社は空欄(社数が減ってよい)
BANNED = re.compile(r"拝見|共感|惹か|素敵|魅力的|素晴らしい|感銘|感動|敬服|感服|ご立派|ぜひご覧|ご覧ください")
QUOTE_MARKS = re.compile(r"[『』「」]")
NEGATED = re.compile(r"ません|ていない|いない|ない|なかっ|なく|ず|たら|れば")  # 根拠の語の直後の否定・条件。前提の根拠にしない
# 第1文が会社を評価して終わる述語。これで終わる第1文は不合格
BAD_FIRST_END = re.compile(r"(?:大切に(?:され|し)|目指し|こだわっ|力を入れ|心がけ|心掛け)て(?:い|おられ)ます。$")
SENDER_ACT = re.compile(r"ご提案|ご用意|ご連絡")
# 方向検査: 時間(施術中・前後・待ち・滞在・合間)を舞台にした文は不合格
TIME_STAGE = re.compile(r"施術中|前後|待ち時間|お待ち|待つ時間|滞在|合間|お過ごしの|お立ち寄りの際|お時間に|ひととき|楽しんでいただく")
# 3目的(施策が効く先)。第2文がどれかに直結して言い切れていること
GOALS = {
    "新規": re.compile(r"新しいお客様|初めて(?:の方|来店|ご来店|参加)|新規"),
    "リピーター": re.compile(r"リピート|繰り返し|再来店|また(?:足を運|来店|ご来店)"),
    "接触回数": re.compile(r"お会いする(?:回数|機会)|接点"),
}
PROPOSAL_TERMS = ("制作体験", "ワークショップ", "体験", "イベント")


def _s(text):
    return lambda kw: text


# (場面名, 前提の根拠の語, 第1文の前提, 第1文の施策, 主軸の目的, 第2文の導入, 提案の語の優先, 第2文の言い切り, 土台にした提案書の箇所, 提案書の言葉(第2文に必ず入れる))
# 前提=その会社が人の集まる催しを持てる理由。第2文の言い切りは案件の企業向け提案書の原稿の言葉に立脚する。提案書に無い効果は書かない
# スライド7(待ち時間の活用)は住宅・自動車などの同伴者・商談待ちの章なので、美容室には当てない
SCENES = [
    ("家族連れ", re.compile(r"家族|ご友人|親子|お子"), "ご家族やご友人と安心して過ごせる場を目指されている御社へ", "ご友人やご家族を誘って来店していただく理由をつくる施策",
     "リピーター", "リピーター向けの催しとして、常連のお客様がご家族やご友人を誘って参加できる、", ("ワークショップ", "イベント"),
     "再来店を楽しみにしていただく理由になるとともに、お連れ様という新しいお客様との出会いにもなります", "スライド17(リピーター向け企画)・スライド3の07と08", ("再来店を楽しみにしていただく", "新しいお客様との出会い")),
    ("気軽な来店", re.compile(r"気軽に(?:お)?立ち寄"), "気軽に立ち寄られる街のお店として親しまれている御社へ", "まだ来店されたことのない近隣の方に、足を運んでいただくきっかけをつくる施策",
     "新規", "ふらりと足を運べる雰囲気のまま、初めての方も参加できる、", ("制作体験", "体験"),
     "お店を訪れる、もうひとつの理由になり、新しいお客様との出会いが生まれます", "スライド6(店舗への来店促進)・スライド3の01と08", ("お店を訪れる、もうひとつの理由", "新しいお客様との出会い")),
    ("地域のお客様", re.compile(r"地域|近隣"), "地域のお客様に通われている御社へ", "地域の方との接点を増やす施策",
     "接触回数", "施術とは別の日に、地域の方が会い、話し、体験できる、", ("ワークショップ", "イベント"),
     "お店と地域の方との新しい接点になり、継続的な関係づくりにつながります", "スライド4(リアルな接点の価値)・スライド17", ("新しい接点", "継続的な関係づくり")),
    ("お客様とのつながり", re.compile(r"つながり|交流|コミュニティ|一期一会"), "お客様とのつながりを大切にされている御社へ", "そのつながりを来店の間隔の外でも育てる施策",
     "接触回数", "日頃の感謝を伝える交流の時間として、施術とは別の日に開く、常連のお客様向けの", ("制作体験", "ワークショップ", "体験"),
     "継続的な接点が増え、再来店を楽しみにしていただく関係になります", "スライド17(顧客感謝イベント)・スライド4", ("日頃の感謝を伝える交流の時間", "再来店を楽しみにしていただく")),
    ("長く通うお客様", re.compile(r"いくつになっても|維持できる|長く通"), "美しい髪を長く維持したい方が通われる御社へ", "ご来店のない月にもお会いする機会をつくる施策",
     "接触回数", "季節ごとの継続企画として、通われているお客様向けに別日に開く、", ("制作体験", "ワークショップ", "体験"),
     "お会いする機会が増え、再来店を楽しみにしていただく理由になります", "スライド17(季節ごとの継続企画)・スライド3の07", ("季節ごとの継続企画", "再来店を楽しみにしていただく")),
    ("専門を掲げるサロン", re.compile(r"ヘッドスパ|頭皮"), "ヘッドスパを軸に掲げているサロンへ", "ヘッドスパをまだ知らない方に来店していただくきっかけをつくる施策",
     "新規", "お花に触れる体験を入口にして、初めての方が気軽に参加できる、", ("ワークショップ", "イベント"),
     "ブランドへの親しみを深める機会になり、新しいお客様との出会いが生まれます", "スライド15(美容・コスメ)・スライド3の04と08", ("ブランドへの親しみを深める", "新しいお客様との出会い")),
]
ANCHORS = tuple(w for sc in SCENES for w in sc[9])


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


def goals_of(opener):
    """第2文が直結して言い切れている3目的のうち、当てはまるもの(方向検査)"""
    parts = [x for x in (opener or "").split("。") if x]
    return [g for g, rx in GOALS.items() if len(parts) == 2 and rx.search(parts[1])]


def valid_opener(opener, template=""):
    """v4 の合格条件: 2文・第1文に送り手の行為・第1文が会社評価の述語で終わらない・禁止語と引用符なし・時間を舞台にしない・第2文が3目的のどれかを言い切る・第2文に文面の提案語"""
    parts = [x for x in (opener or "").split("。") if x]
    if len(parts) != 2 or not SENDER_ACT.search(parts[0]) or BAD_FIRST_END.search(parts[0] + "。"):
        return False
    if BANNED.search(opener) or QUOTE_MARKS.search(opener) or TIME_STAGE.search(opener) or not goals_of(opener) or not any(w in parts[1] for w in ANCHORS):  # 第2文は提案書の言葉を使う(提案書照合)
        
        return False
    return not template or any(p in parts[1] for _t, p in proposal_phrases(template, PROPOSAL_TERMS, strict=True))


def opener_traces(basis, template=""):
    """(書き出し, {根拠, 前提, 主軸の目的, 提案物}) を候補の順に出す。根拠の語(basis にある)が第1文の前提を決め、前提が第2文の目的を決める。
    前提が拾えない・文面に提案語が無い会社は何も出さない(汎用の接続で埋めない)"""
    text = basis or ""
    for scene, rx, premise, plan, goal, intro, terms, effect, ref, _anchors in SCENES:
        for m in rx.finditer(text):
            if NEGATED.search(re.split(r"[。、]", text[m.end():m.end() + 16])[0]):  # 同じ句の中(読点・句点まで)の否定・条件
                continue
            first = f"{premise}、{plan}をご提案したく、ご連絡しました。"
            for term, phrase in proposal_phrases(template, terms, strict=True):
                out = f"{first}{intro}{phrase}を開催し、{effect}。"
                if valid_opener(out):
                    yield out, {"根拠": m.group(0), "前提": scene, "主軸": goal, "提案物": phrase, "提案書": ref}
            break  # 同じ前提の2つ目以降の語は同じ第1文を作りやすいので、前提ごとに最初の語だけ


FALLBACK_FIRST = "御社に、新しいお客様との接点を増やす施策をご提案したく、ご連絡しました。"
FALLBACK_SECOND = "{phrase}を開催し、{shop}を訪れる、もうひとつの理由になり、新しいお客様との出会いが生まれます。"  # 企業向け提案書のスライド6(店舗への来店促進)・スライド3の01と08の言葉
FALLBACK_SECOND_NO_SHOP = "{phrase}を開催し、新しいお客様との出会いが生まれます。"  # 文面に「お店」「店舗」の語が無いとき(語を足さない)


def fallback_opener(template=""):
    """ホームページから前提が取れない会社に入れる、業種の文面に沿った自動の下書き。
    商材の語(提案の語・「お店」)は送る文面からだけ取る。取れなければ空文字(固定の語で埋めない。呼び出し側が空欄と理由を出し、送る対象に入れない)。
    会社の特徴は言えないので、第1文は施策の提案だけにする。方向検査(時間を舞台にしない・3目的のどれかを言い切る)は通す。画面では「書き出しは自動の下書き」と分かる印を付ける"""
    found = proposal_phrases(template, PROPOSAL_TERMS, strict=True)
    if not found:
        return ""
    shop = next((w for w in ("お店", "店舗") if w in (template or "")), "")
    second = (FALLBACK_SECOND.format(phrase=found[0][1], shop=shop) if shop else FALLBACK_SECOND_NO_SHOP.format(phrase=found[0][1]))
    out = FALLBACK_FIRST + second
    return out if valid_opener(out) else ""


def opener_variants(basis, template=""):
    for out, _t in opener_traces(basis, template):
        yield out


def first_sentence_form(opener):
    """第1文の「型」: 前提(御社へ)より後ろの、施策の句まで。他社の第1文と入れ替えて成立する文は、この型が同じになる"""
    return first_sentence(opener).split("、", 1)[-1]


def opener_from_basis(basis, template="", taken=()):
    """最初の合格候補。taken(他の会社の書き出し)と、2文とも同じもの・第1文が同じもの・第1文の型が同じものは飛ばす。無ければ空文字"""
    taken_first = {first_sentence(t) for t in taken if t}
    taken_form = {first_sentence_form(t) for t in taken if t}
    for out in opener_variants(basis, template):
        if out not in taken and first_sentence(out) not in taken_first and first_sentence_form(out) not in taken_form:
            return out
    return ""
