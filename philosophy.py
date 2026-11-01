"""会社の方針・思いを、ホームページの本文から拾い、書き出しの根拠にする(2026-11-02 運用者の指示「会社の方針思いを拾って文章つくるように」)。
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
        f, h = site.get(u)
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



# ---- v2(2026-11-02): 書き出しは2文。第1文=相手の特徴を事実として言い切る、第2文=送る文面の提案に接続して提案で締める ----
# 禁止: 拝見・共感・お世辞の語、根拠文の引用貼り付け(『』「」)、どの会社にも使える接続文
BANNED = re.compile(r"拝見|共感|惹か|惹か|素敵|魅力的|素晴らしい|感銘|感動|敬服|感服|ご立派|ぜひご覧|ご覧ください")
QUOTE_MARKS = re.compile(r"[『』「」]")

# 特徴の言い切り文を作る規則: (根拠文中の手がかり, 手がかりの前の語句 head を使う文の型)。head は手がかり直前の読点までの語句
FACT_RULES = [
    (re.compile(r"にこだわ"), "{h}にこだわっておられます。"),
    (re.compile(r"を大切に"), "{h}を大切にされています。"),
    (re.compile(r"に寄り添"), "{h}に寄り添うことを大切にされています。"),
    (re.compile(r"を目指"), "{h}を目指しておられます。"),
    (re.compile(r"を使命|を目標"), "{h}を使命にされています。"),
    (re.compile(r"に貢献"), "{h}への貢献を大切にされています。"),
    (re.compile(r"丁寧に仕上げ"), "丁寧に仕上げることを大切にされています。"),
    (re.compile(r"を心がけ|を心掛け"), "{h}を心がけておられます。"),
    (re.compile(r"をお届け|をご提供|を提供"), "{h}をお届けすることに力を入れておられます。"),
]
_LEAD = re.compile(r"^(?:そして|また|さらに|なにより|何よりも|私たちは|わたしたちは|当店は|当社は|当サロンは)")

# 特徴の種類 → 第2文の「接続」。上から順に最初に当たったものを使う。どれにも当たらない会社は作らない(汎用の接続を禁止するため)
CONNECT = [
    ("family", re.compile(r"家族|ご友人|親子|お子"), "ご家族やご友人と過ごすその機会に、", ("ワークショップ", "体験", "イベント")),
    ("relax", re.compile(r"癒し|くつろ|リラックス|ゆっくり|安心|落ち着|心地"), "その過ごしていただく時間に、", ("制作体験", "体験", "ワークショップ")),
    ("care", re.compile(r"ケア|トリートメント|施術|カラー|パーマ|薬剤|仕上がり|技術|髪|頭皮|ダメージ"), "その施術やケアの時間に合わせて、", ("制作体験", "体験", "ワークショップ")),
    ("polite", re.compile(r"丁寧|クオリティ|品質|確か|短い時間"), "ご来店を機に、", ("記念品", "ギフト", "特典", "制作体験", "体験")),
    ("town", re.compile(r"地域|街|寄り添|近隣"), "地域のお客様との接点づくりとして、", ("ワークショップ", "イベント", "体験")),
    ("people", re.compile(r"社会|貢献|スタッフ|成長|価値観|理想"), "お客様やスタッフとの関わりを深める場として、", ("ワークショップ", "体験", "イベント")),
]
# 提案の具体語(その文面に実際にある語だけを使う)
PROPOSAL_TERMS = ("制作体験", "ワークショップ", "体験", "イベント", "ギフト", "記念品", "特典")


def _head_before(clause, cue):
    m = cue.search(clause)
    if not m:
        return ""
    h = clause[:m.start()]
    h = re.split(r"では|は", h)[-1] if re.search(r"では|は", h) and len(re.split(r"では|は", h)[-1]) >= 2 else h
    h = re.split(r"[ 　]|よう", h)[-1] if re.search(r"[ 　]|よう", h) and len(re.split(r"[ 　]|よう", h)[-1]) >= 3 else h
    h = _LEAD.sub("", h.strip())
    h = QUOTE_MARKS.sub("", h).strip(" 　,、")
    return h


def fact_sentence(basis):
    """根拠文から、相手の特徴を事実として言い切る第1文を作る(引用を貼らない・感想を入れない)。作れなければ ""。
    手がかり(にこだわ・を大切に・に寄り添・を目指 等)の直前の語句を使うので、根拠に無い特徴は足さない"""
    for sent in [x for x in (basis or "").split(" / ") if x.strip()]:
        s = re.sub(r"(?<=[^\x00-\x7f]) +(?=[^\x00-\x7f])", "", sent.strip().rstrip("。！？!?").strip())
        for clause in [c for c in re.split(r"[、，,。]", s) if c.strip()]:
            clause = re.sub(r"^(?:[0-9０-９]{1,2}\s*[.．)）、]\s*|[①-⑩]\s*)", "", clause.strip())
            for cue, frame in FACT_RULES:
                h = _head_before(clause, cue)
                if 2 <= len(h) <= 30 and specific_nouns(h) and not THANKS_ONLY.search(h):
                    return frame.format(h=h)
    return ""


THANKS_ONLY = re.compile(r"^(?:お客様|お客さま|皆様)(?:の|に|を)?$")


def proposal_phrases(template, kind_terms):
    """送る文面(template)に実際にある提案の具体語を、相手の特徴に合う順に並べた [(語, 提案の句)]。同じ句は1つ。無ければ []"""
    text = template or ""
    base = "本物のお花を使った" if ("本物のお花" in text or "本物の花" in text) else ""
    out, seen, spans = [], set(), []
    for term in tuple(kind_terms) + PROPOSAL_TERMS:
        m = re.search(r"(?:(?!企業向けの)[一-龥ぁ-んァ-ヶー]{1,4}の)?" + re.escape(term) + ("会?" if term == "体験" else ""), text)  # 「企業向けの」は相手の会社を指す語で、書き出しでは不自然なので前置きに取らない
        if not m or (base + m.group(0)) in seen:
            continue
        pos = text.find(term, m.start())  # 語そのものの位置。別の語の一部(制作体験の「体験」)なら、同じ提案の言い換えなので候補にしない
        if any(a <= pos < b for a, b in spans):
            continue
        spans.append((m.start(), m.end()))
        seen.add(base + m.group(0))
        out.append((term, base + m.group(0)))
    return out


def opener_variants(basis, template=""):
    """根拠文と、その会社に送る文面から作れる2文の書き出しを、候補の順に出す(第2文の提案語を変えた候補が続く)。作れなければ何も出さない。
    第1文: 相手の特徴の言い切り(fact_sentence)。第2文: 特徴の種類に合う接続 + 文面にある提案の具体語 + 「を取り入れる企画をご提案できます。」
    同じ書き出しが他の会社に既にあるとき、呼び出し側が次の候補を使う(2文とも同一の行を作らないため)"""
    first = fact_sentence(basis)
    if not first:
        return
    for ctx in (first, basis or ""):  # まず第1文の特徴から接続を決め、当たらなければ根拠文全体から
        for _kind, rx, connect, terms in CONNECT:
            if rx.search(ctx):
                for _term, phrase in proposal_phrases(template, terms):
                    out = f"{first}{connect}{phrase}を取り入れる企画をご提案できます。"
                    if not (BANNED.search(out) or QUOTE_MARKS.search(out)):
                        yield out
                return


def opener_from_basis(basis, template="", taken=()):
    """opener_variants の最初の候補。taken(他の会社の書き出し)にあるものは飛ばす。使える候補が無ければ空文字"""
    for out in opener_variants(basis, template):
        if out not in taken:
            return out
    return ""
