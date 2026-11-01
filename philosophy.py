"""会社の方針・思いを、ホームページの本文から拾い、書き出しの根拠にする(2026-11-02 運用者の指示「会社の方針思いを拾って文章つくるように」)。
拾うのはトップページの見出し・段落と、理念・コンセプト・こだわり等のリンク先(同じホストの最大2ページ)。
書き出しは根拠の文の引用だけで作る(言い換えて意味を変えない・書いていないことを足さない)。拾えない会社は空のまま、理由を言う。
実行環境: Windows / Linux / macOS (純粋な文字列処理。取得は site_checker.Site を呼ぶ側が渡す)"""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# 方針・思いの語(強い順に重み)。これを含む文を最優先で拾う
THOUGHT = re.compile(r"理念|想い|思い|信念|モットー|ポリシー|大切に|こだわ|目指|めざ|心がけ|心掛け|寄り添|ミッション|ビジョン|お届け|守り|守る|ひとり.{0,3}ひとり|一人.{0,3}一人|ために|ことを第一|第一に|方針|目標|理想|成長|貢献|使命|価値観|誇り|姿勢|約束|考え方")
# 理念型・こだわり型・特徴型の見分け(決定的な規則)
KIND_RIDEN = re.compile(r"理念|想い|思い|信念|モットー|ポリシー|ミッション|ビジョン|目指|めざ|理想|使命|目標|方針|価値観")
KIND_KODAWARI = re.compile(r"こだわ|大切に|心がけ|心掛け|寄り添|第一に|守")

# 候補から外す文(予約・クーポン・料金・営業時間・アクセス・求人・プライバシー・ナビ)
EXCLUDE = re.compile(r"予約|クーポン|キャンペーン|キャンペイン|料金|価格|円|税込|営業時間|定休|アクセス|求人|採用|スタッフ募集|プライバシー|個人情報|"
                     r"お問い?合わ?せ|TEL|Tel|tel|電話|〒|住所|ログイン|カート|Copyright|©|メニュー|ご利用ください|詳しくはこちら|"
                     r"クリック|タップ|ホットペッパー|HOT PEPPER|ポイント|割引|OFF|%|％|SALE|セール|ブログ更新|お知らせ|NEWS|news|￥|¥|感染|営業再開|通常営業|短縮営業|休業|緊急事態|コロナ|ウイルス|消毒|購入|通販|ショップ|ストア|販売|送料|在庫|人材|求めています|一緒に働|仲間を", re.I)
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


def quote_of(sentence):
    """根拠の文から、書き出しに入れる引用(12〜52字)を決める。長い文は読点で区切った、方針・思いの語を含む部分だけ。作れなければ空"""
    s = sentence.strip().rstrip("。！？!?").strip()
    s = re.sub(r"^[\s　・■◆●◎▶▷→※]+", "", s)
    s = re.sub(r"^(?:[0-9０-９]{1,2}\s*[.．)）、]\s*|[①-⑩]\s*)", "", s)  # 見出しの番号「1. 」
    s = re.sub(r"(?<=[^\x00-\x7f]) +(?=[^\x00-\x7f])", "", s)  # 日本語どうしの間の空白(ページの改行跡)
    if QUOTE_MIN <= len(s) <= QUOTE_MAX:
        return s
    clauses = [c.strip() for c in re.split(r"[、，,]", s) if c.strip()]
    best = ""
    for n in range(len(clauses), 0, -1):  # 長い連続から試し、QUOTE_MAX に収まる最長の連続を探す
        for i in range(0, len(clauses) - n + 1):
            seg = "、".join(clauses[i:i + n])
            if QUOTE_MIN <= len(seg) <= QUOTE_MAX and THOUGHT.search(seg) and specific_nouns(seg) and END_OK.search(seg):
                if len(seg) > len(best):
                    best = seg
        if best:
            return best
    return ""


def opener_from_basis(basis):
    """根拠文(' / ' 区切りで最大2文)から書き出しを作る。作れなければ ""。引用は根拠の文そのまま。
    型: 理念型 / こだわり型 / 特徴型。機械条件: 40〜90字・会社固有の語を1つ以上含む"""
    for sent in [x for x in (basis or "").split(" / ") if x.strip()]:
        q = quote_of(sent)
        if not q or not specific_nouns(q):
            continue
        if KIND_RIDEN.search(q):
            out = f"ホームページで『{q}』という想いを拝見し、共感いたしました。"
        elif KIND_KODAWARI.search(q):
            out = f"『{q}』という姿勢を拝見し、ご連絡いたしました。"
        else:
            out = f"『{q}』という取り組みを拝見しました。"
        if 40 <= len(out) <= 90:
            return out
    return ""
