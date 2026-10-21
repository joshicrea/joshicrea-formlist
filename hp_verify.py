"""検索で見つけたHPが、本当にその会社のものかを、地図が持つ電話番号・住所で照合する。実行環境: Windows / Python 3.9+。
証拠の強さ(強い順): 電話一致 / 住所一致 / 名前のみ(題名・見出しに会社名) / 不一致。
電話か住所が一致すれば別会社のHPではない。名前だけの一致は、名前が短い・英単語だけの会社(su・mimi・Prune・庄司など)では採らない。
陽性対照: 電話 03-1234-5678 の会社が、本文に「03-1234-5678」「０３（１２３４）５６７８」のあるページで電話一致になる。
陰性対照: 辞書・ニュース・技術記事のページ(電話も住所も会社名も無い)は不一致になる。
原理的に取りこぼす型: 電話・住所を画像で載せている店(名前のみになる)、地図側に電話・住所が無い会社、
予約サイトの個別ページ(電話は載るが店のHPではない。電話が多数載る一覧ページは弾くが、1店だけのページは照合では弾けない)。
  既知の予約・掲載・口コミ・SNSのドメインは、照合の前に site_checker.SOCIAL の表で外す(地図に登録のHPも検索で見つけたHPも)。
  表に無い掲載サイトの1店ページは、この照合を通ってしまう。"""
import re
import unicodedata

from limited_http_budget import HttpBudgetExceeded

STRONG = ("電話一致", "住所一致")
PHONE = re.compile(r"(?<!\d)0\d{1,4}[-ー−‐―‑\s().（）]{0,3}\d{1,4}[-ー−‐―‑\s().（）]{0,3}\d{3,4}(?!\d)")
LISTING_PHONES = 8  # 本文にこれより多い電話番号が載るページは、店のHPではなく一覧ページとみなす


def _digits(s):
    return re.sub(r"\D", "", unicodedata.normalize("NFKC", s or ""))


def tel_digits(tel):
    """地図の電話表記を 0 から始まる数字列にそろえる(+81 3-... -> 03...)。10〜11桁でなければ空"""
    d = _digits(tel)
    if d.startswith("81") and len(d) in (11, 12):
        d = "0" + d[2:]
    return d if len(d) in (10, 11) and d.startswith("0") else ""


def phones_in(text):
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"\+81[\s\-()]*0?[\s\-()]*(\d)", r"0\1", t)  # +81 3-xxxx -> 03-xxxx
    found = set()
    for m in PHONE.finditer(t):
        d = _digits(m.group())
        if len(d) in (10, 11):
            found.add(d)
    return found


_KANJI = {c: i for i, c in enumerate("〇一二三四五六七八九")}


def _kanji_num(t):
    """一〜九十九の漢数字を整数に。読めなければ None"""
    if not t or any(c not in "〇一二三四五六七八九十" for c in t):
        return None
    if "十" in t:
        head, _, tail = t.partition("十")
        return (_KANJI[head] if head else 1) * 10 + (_KANJI[tail] if tail else 0) if (len(head) <= 1 and len(tail) <= 1) else None
    return int("".join(str(_KANJI[c]) for c in t))


def _addr_norm(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[ー−‐―‑–—]", "-", s)
    s = re.sub(
        r"(?<!条西)(?<!条東)([一二三四五六七八九十〇]{1,3})丁目",
        lambda m: (f"{_kanji_num(m.group(1))}丁目" if _kanji_num(m.group(1)) is not None else m.group(0)),
        s,
    )
    s = re.sub(r"(\d)\s*-\s*(?=\d)", r"\1-", s)  # 「1 - 15」「1- 15」を「1-15」に(区切りの前後の空白だけ)
    s = re.sub(r"(?<!条西)(?<!条東)(\d)丁目", r"\1-", s)
    s = re.sub(r"(\d)番地?", r"\1-", s)
    s = re.sub(r"(\d)の(?=\d)", r"\1-", s)
    s = re.sub(r"(\d)号", r"\1", s)
    s = re.sub(r"(?<=[^\x00-\x7f\d]) (?=\d)", "", s)  # 「門前仲町 1-15」の町名と番地の間の空白だけ消す。番地のあとの空白(階数などとの区切り)は残す
    return re.sub(r"-{2,}", "-", s)


def addr_key(address):
    """住所から「町名+丁目+番」の鍵を作る。東京都江東区門前仲町1-15-2 グレイス -> 門前仲町1-15。番地が2つ揃わなければ空
    (「1丁目」までの住所や、区切りなしに連結された数字は、隣の番地と取り違えるので鍵にしない)"""
    s = _addr_norm(address)
    s = re.sub(r"^.{2,3}?[都道府県]", "", s, count=1)
    s = re.sub(r"^.+?[市区町村郡]", "", s, count=1)
    m = re.match(r"([^\d]{2,10})(\d+)-(\d+)", s)
    return f"{m.group(1)}{m.group(2)}-{m.group(3)}" if m else ""


def weak_name(name, area=""):
    """名前だけでは同じ会社と言えない名前: 塊が無い・英字だけ(2塊以下)・合計6文字以下"""
    from hp_finder import _tokens
    toks = _tokens(name, area)
    if not toks:
        return True
    if all(t.isascii() for t in toks) and len(toks) <= 2:
        return True
    return sum(len(t) for t in toks) <= 6


# 名前だけの一致で採るときに、本文に出ているはずの言葉(業種ごと)。austin(米国の都市)・abc-mart・autodesk のように
# 名前の最初の語だけが同じ別の会社を弾く。ここに無い業種は、この条件を掛けない(弾き過ぎないため)
INDUSTRY_WORDS = {
    "サロン": r"美容|ヘア|サロン|ネイル|エステ|まつ毛|まつげ|アイラッシュ|脱毛|カット|カラー|パーマ|リラク|マッサージ|スパ|整体|ブライダル",
    "美容室": r"美容|ヘア|サロン|カット|カラー|パーマ|トリートメント|スタイリスト",
    "ネイルサロン": r"ネイル|ジェル|アート|サロン|美容",
    "エステサロン": r"エステ|フェイシャル|痩身|脱毛|サロン|美容|リラク|マッサージ",
    "ホテル・旅館": r"宿泊|客室|チェックイン|ご予約|ホテル|旅館|温泉|宿|アクセス",
    "ケアハウス": r"ケアハウス|介護|入居|施設|老人ホーム|軽費老人ホーム|特定施設|生活介護|ユニットケア",
    "ブライダル": r"結婚式|挙式|ウェディング|ブライダル|チャペル|披露宴|婚礼|式場",
    "住宅・不動産": r"住宅|不動産|物件|賃貸|注文住宅|土地|建築|分譲|リフォーム",
    "福祉施設": r"介護|福祉|デイサービス|入居|利用者|施設|ケア",
}
INDUSTRY_ALIAS = {"美容・コスメ": "サロン"}  # 同じ地図タグを使う別名の業種
# ここに無い業種は、本文の言葉では確かめられない。その業種では名前だけの一致を採らず、電話か住所の一致を必須にする


def _industry_ok(row, text):
    """True/False=業種の言葉が本文に3種類以上あるか。None=この業種は本文の言葉で確かめられない"""
    ind = row.get("industry", "")
    pat = INDUSTRY_WORDS.get(ind) or INDUSTRY_WORDS.get(INDUSTRY_ALIAS.get(ind, ""))
    if not pat:
        return None
    # 1語だけの一致(「スパークリング」の「スパ」など)では足りない。別の語が3種類以上、本文に出ていること
    return len(set(re.findall(pat, unicodedata.normalize("NFKC", text or "")))) >= 3


def _detail(level, why, **kw):
    d = {
        "level": level,
        "why": why,
        "category": kw.pop("category", ""),
        "tel_hit": kw.pop("tel_hit", ""),
        "addr_status": kw.pop("addr_status", ""),
        "addr_why": kw.pop("addr_why", ""),
        "title_match": kw.pop("title_match", ""),
        "industry_ok": kw.pop("industry_ok", ""),
    }
    d.update(kw)
    return d


def _title_has_name_token(title, token):
    if token in title:
        return True
    if not token.isascii() and token.endswith("店"):
        base = token[:-1]
        if len(base) >= 2 and base in title:
            return True
    return False


FACILITY_SPECIFIC_INDUSTRIES = {"ケアハウス"}
FACILITY_GENERIC_TOKENS = {
    "介護専用型", "ケアハウス", "軽費老人ホーム", "特定施設", "生活介護",
    "老人ホーム", "特別養護老人", "特別養護老人ホーム", "施設", "福祉施設",
    "記念",
}


def facility_specific_name_only_mismatch(row, text, titles):
    """ケアハウス系の名前のみ採用では、サービス種別だけでなく施設固有名を要求する。
    例: 「介護専用型ケアハウスあかね」に対して「介護専用型ケアハウスひまわり」は、
    電話・住所一致が無ければ別施設として扱う。
    """
    if row.get("industry", "") not in FACILITY_SPECIFIC_INDUSTRIES:
        return ""
    from hp_finder import _norm, _tokens
    toks = _tokens(row.get("name", ""), row.get("area", ""))
    specific = [t for t in toks if t not in FACILITY_GENERIC_TOKENS]
    if not specific:
        return "題名にサービス種別はあるが、施設固有名が会社名にない（電話か住所の一致が必要）"
    haystack = _norm((titles or "") + " " + (text or ""))
    if any(t in haystack for t in specific):
        return ""
    return "題名にサービス種別はあるが、施設固有名が本文・題名にない（電話か住所の一致が必要）"


def partial_ascii_name_only_mismatch(row, titles):
    """英字名+店舗名の名前のみ採用では、タイトルに全トークンが必要。

    Austin 門前仲町 -> Austin official のように先頭の英字だけ合う候補は、
    電話・住所が無ければ別会社や地名に寄ったページを採りやすい。
    日本語の店舗名だけは「門前仲町店」と「門前仲町」の差を同一表記として許容する。
    """
    from hp_finder import _norm, _tokens

    toks = _tokens(row.get("name", ""), row.get("area", ""))
    if len(toks) == 2 and any(t.isascii() for t in toks):
        title = _norm(titles)
        if not all(_title_has_name_token(title, t) for t in toks):
            return "題名に会社名の一部しかなく、電話・住所なしでは同じ会社と言えない"
    return ""


def judge_detail(row, text, titles=""):
    """judge と同じ結論に、再検証用の分解根拠を足す。採用閾値は変えない。"""
    from hp_finder import same_company
    phones = phones_in(text)
    if len(phones) > LISTING_PHONES:
        return _detail("不一致", f"電話番号が{len(phones)}件載る一覧ページ", category="clear_conflict")
    tel = tel_digits(row.get("tel", ""))
    tel_hit = (tel in phones) if tel else "unknown"
    addr, addr_why = addr_evidence(row.get("address", ""), text)
    if not row.get("address", ""):
        addr = "unknown"
    title_match = (same_company(row.get("name", ""), titles, row.get("area", "")) if titles else "unknown")
    if addr == "食い違い":
        why = ("電話は一致するが、" if tel_hit is True else "") + "住所が食い違う（" + addr_why + "）"
        return _detail("不一致", why, category="clear_conflict", tel_hit=tel_hit,
                       addr_status=addr, addr_why=addr_why, title_match=title_match)
    if tel_hit is True:
        return _detail("電話一致", "電話番号が本文にある" + ("・" + addr_why if addr == "一致" else ""),
                       category="strong_match", tel_hit=True, addr_status=addr,
                       addr_why=addr_why, title_match=title_match)
    if addr == "一致" and not (tel and phones):
        return _detail("住所一致", addr_why, category="strong_match", tel_hit=tel_hit,
                       addr_status=addr, addr_why=addr_why, title_match=title_match)
    if title_match is True:
        partial_why = partial_ascii_name_only_mismatch(row, titles)
        if partial_why:
            return _detail("不一致", partial_why,
                           category="needs_review", tel_hit=tel_hit, addr_status=addr,
                           addr_why=addr_why, title_match=False)
        facility_why = facility_specific_name_only_mismatch(row, text, titles)
        if facility_why:
            return _detail("不一致", facility_why,
                           category="needs_review", tel_hit=tel_hit, addr_status=addr,
                           addr_why=addr_why, title_match=True, facility_specific=False)
        if tel and phones and tel_hit is False and addr != "一致":  # 会社の電話が分かっていて、ページには別の番号だけが載り、住所の裏づけも無い=同じ名前の別の会社(例: 札幌の同名店)。住所が一致するなら従来どおり名前のみで残す
            return _detail("不一致", "ページの電話番号が、この会社の電話と違う（同じ名前の別の会社の疑い）",
                           category="needs_review", tel_hit=False, addr_status=addr,
                           addr_why=addr_why, title_match=True)
        ok = _industry_ok(row, text)
        if ok is None:
            return _detail("不一致", "題名に会社名はあるが、この業種は本文の言葉で確かめられない（電話か住所の一致が必要）",
                           category="needs_review", tel_hit=tel_hit, addr_status=addr,
                           addr_why=addr_why, title_match=True, industry_ok="unknown")
        if not ok:
            return _detail("不一致", "題名に会社名はあるが、本文に業種の言葉が3種類以上ない（同じ名前の別の会社・地名の疑い）",
                           category="needs_review", tel_hit=tel_hit, addr_status=addr,
                           addr_why=addr_why, title_match=True, industry_ok=False)
        return _detail("名前のみ", "題名・見出しに会社名があり、業種の言葉も本文にある（電話・住所は確認できず）",
                       category="needs_review", tel_hit=tel_hit, addr_status=addr,
                       addr_why=addr_why, title_match=True, industry_ok=True)
    return _detail("不一致", "電話・住所・会社名のどれも見つからない",
                   category="needs_review", tel_hit=tel_hit, addr_status=addr,
                   addr_why=addr_why, title_match=title_match)


def title_mismatch_detail(detail, why):
    """強い一致をタイトル不一致で止めるとき、上書き前の根拠を残す。"""
    d = dict(detail or {})
    d["pre_override_level"] = d.get("level", "")
    d["pre_override_why"] = d.get("why", "")
    d["level"] = "不一致"
    d["why"] = why
    d["category"] = "needs_review"
    if d.get("title_match") != "unknown":
        d["title_match"] = False
    return d


def addr_nums(address):
    """住所の (市区町村, 町名, 番地の数字の並び) を返す。東京都江東区門前仲町1-15-2 -> ("江東区", "門前仲町", ("1","15","2")) 。数字が2つ未満なら None"""
    s = _addr_norm(address)
    s = re.sub(r"^\s*日本[、,]?\s*", "", s)
    s = re.sub(r"^\s*〒?\s*\d{3}-?\d{4}\s*", "", s)  # 先頭の郵便番号
    s = re.sub(r"^.{2,3}?[都道府県]", "", s, count=1)
    cm = re.match(r"(.+?市(?!市)(?:[^\d市区 ]{1,5}区)?|.+?[区町村郡])", s)  # 四日市市・大町市・札幌市中央区・大阪市北区を、市(と区)までで切る
    city = cm.group(1) if cm else ""
    s = s[len(city):]
    m = re.match(r"((?:北|南)?\d+条[東西]\d+丁目)(\d+(?:-\d+)*)", s)
    if not m:
        m = re.match(r"([^\d ]{2,10})(\d+(?:-\d+)*)", s)
    if not m:
        return None
    nums = tuple(m.group(2).split("-"))
    return (city, m.group(1), nums) if len(nums) >= 2 else None


def addr_evidence(address, text):
    """住所の照合。戻り値: ("一致" | "食い違い" | "なし", 詳細)。
    ページ内の「同じ町名+番地」を拾う。町名は区切り(先頭・市区町村・空白・記号)から始まるものだけを数える
    (「日本橋本町」の中の「本町」は別の町)。直前の市区町村が地図の住所と違えば、別の土地の同名の町として数えない。
    共通して比べられる桁(2桁以上)がすべて同じなら一致。1つも一致せず、比べられる記載が1つ以上あれば食い違い。
    地図の住所が番地2つ(1-15)でページが3つ(1-15-2)のものは、同じ建物と言えないので証拠にしない。無ければ なし"""
    an = addr_nums(address)
    if not an:
        return "なし", ""
    city, town, nums = an
    t = _addr_norm(text)
    seen = []
    for m in re.finditer(re.escape(town) + r"(\d+(?:-\d+)*)(?!\d)", t):
        i = m.start()
        prev = t[i - 1] if i else ""
        if prev and prev in "市区町村郡":
            if city and not t[:i].endswith(city):
                continue  # 別の市区町村の同名の町
        elif prev and not (prev.isspace() or prev in ",、。・:：;；()（）[]【】「」『』<>/|\"'" or prev == "-" or prev.isascii() and not prev.isalnum()):
            continue  # 「日本橋本町」の「本町」のように、別の町名の一部
        page = tuple(m.group(1).split("-"))
        n = min(len(nums), len(page))
        if n < 2:
            continue
        if {len(nums), len(page)} == {2, 3}:
            continue  # 片方だけ建物まで分かる記載は、どちらの向きでも同じ建物とは言えない
        if nums[:n] == page[:n]:
            return "一致", f"住所「{town}{'-'.join(nums[:n])}」が本文にある"
        seen.append(town + "-".join(page))
    return ("食い違い", "本文の住所は " + "・".join(dict.fromkeys(seen))[:60]) if seen else ("なし", "")


def judge(row, text, titles=""):
    """戻り値: (レベル, 理由)。row は name・tel・address を持つ辞書。
    証拠(電話の一致・住所の一致・住所の食い違い・会社名)を全部集めてから決める。1つの一致で、他の矛盾を見ずに採らない"""
    d = judge_detail(row, text, titles)
    return d["level"], d["why"]


def usable(row, level):
    """この結果でHPを採ってよいか。強い証拠か、名前が十分に固有なときの名前のみ"""
    return level in STRONG or (level == "名前のみ" and not weak_name(row.get("name", ""), row.get("area", "")))


def quick_text_titles(html):
    """HTMLから本文と題名を正規表現で取る(BeautifulSoup は1ページ数秒かかるので、候補の照合には使わない)。
    戻り値: (本文, 題名・見出し・サイト名)"""
    if len(html) > 600000:
        html = html[:300000] + html[-300000:]  # フッターの電話・住所が末尾にあるので、先頭だけで切らない
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    titles = re.findall(r"(?is)<title[^>]*>(.*?)</title>", body) + re.findall(r"(?is)<h1[^>]*>(.*?)</h1>", body)
    for m in re.finditer(r"(?is)<meta\b[^>]*>", html):
        tag = m.group(0)
        if re.search(r"(?i)(?:property|name)=[\"']?(?:og:site_name|og:title|application-name)", tag):
            c = re.search(r"(?is)content=([\"'])(.*?)\1", tag)
            if c:
                titles.append(c.group(2))
    return text, " ".join(re.sub(r"<[^>]+>|\s+", " ", t).strip() for t in titles)


def check_candidate_detail(url, row, url_allowed=None, budget=None, line_id=None):
    """候補URLのサイトのトップページを1回取得して照合する(深いページ=記事・施設の個別ページ だけを見ない)。
    電話・住所が合っても、サイト名・題名に会社名が無ければ不一致(ニュース・ポータル・施設運営のページの疑い)。
    戻り値: (レベル, 理由, 詳細)。取得できなければ ('取得不可', 理由, 詳細)。ここで起きた例外も '取得不可' にして、探索全体は止めない"""
    from urllib.parse import urlparse

    import site_checker as sc
    from hp_finder import same_company
    try:
        p = urlparse(url)
        site = sc.Site(url, budget=budget, line_id=line_id)
        final, html = site.get(f"{p.scheme}://{p.netloc}/", url_allowed=url_allowed)
        if final is None:
            return "取得不可", html, _detail("取得不可", html, category="fetch_failed", candidate_url=url)
        if url_allowed and not url_allowed(final):
            return "不一致", "blocked by URL policy", _detail("不一致", "blocked by URL policy", category="blocked", candidate_url=final)
        text, titles = quick_text_titles(html)
        detail = judge_detail(row, text, titles)
        detail["candidate_url"] = final
        if detail["level"] not in STRONG and (row.get("tel") or row.get("address")):
            # トップページに電話・住所が無いときだけ、会社概要・アクセスのページを1つ見る(同じホスト・robots・予算の関門は Site が持つ)
            try:
                from bs4 import BeautifulSoup
                more = sc.about_text(BeautifulSoup(html, "html.parser"), final, site, set())
            except HttpBudgetExceeded:
                raise
            except Exception:  # noqa: BLE001
                more = ""
            if more:
                detail = judge_detail(row, text + "\n" + more, titles)
                detail["candidate_url"] = final
        lvl, why = detail["level"], detail["why"]
        if lvl in STRONG and not same_company(row.get("name", ""), titles, row.get("area", "")):
            detail = title_mismatch_detail(detail, "電話・住所は載るが、サイト名が会社名と合わない（ニュース・予約・施設のページの疑い）")
            return detail["level"], detail["why"], detail
        return lvl, why, detail
    except HttpBudgetExceeded:
        raise
    except Exception as e:  # noqa: BLE001
        why = f"error: {type(e).__name__}"
        return "取得不可", why, _detail("取得不可", why, category="fetch_failed", candidate_url=url)


def check_candidate(url, row, url_allowed=None, budget=None, line_id=None):
    """公開互換のため、詳細を落として従来どおり (レベル, 理由) だけ返す。"""
    lvl, why, _detail_obj = check_candidate_detail(url, row, url_allowed=url_allowed, budget=budget, line_id=line_id)
    return lvl, why

