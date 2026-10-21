"""Find the contact form page of a website. Never submits anything (GET only).

Runtime: Windows / cross-platform, Python 3.9+ (requests, beautifulsoup4).

Verdicts:
  フォームあり   a <form> with a textarea or an e-mail field, or an embedded external form
  メールのみ     no form, but a mail address / mailto on the contact page
  見つからず     contact page or form not found
  要目視         page is script-rendered or the form lives in a script widget
  取得失敗       site did not answer
  robots禁止     robots.txt disallows the page
Sales refusal wording on the checked pages sets refuse=True.

Positive control: a site with a known HTML contact form must return フォームあり.
Negative control: a page without any form must return 見つからず / メールのみ.
Known blind spots: forms rendered only by JavaScript, pages behind login,
CAPTCHA-only pages, and refusal wording shown as an image.
"""
import re
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import hp_verify
import netguard

UA = "AiMarunageEigyoTool/1.0 (checks public contact pages only; no submission)"
TIMEOUT = 15
INTERVAL = 1.2  # seconds between requests to the same site

CONTACT_WORDS = re.compile(r"お問い?合わ?せ|問合せ|問い合わせ|ご相談|contact|inquiry|enquiry|toiawase|otoiawase|form", re.I)
STRONG_WORDS = re.compile(r"お問い?合わ?せ|問合せ|問い合わせ|contact|inquiry|toiawase", re.I)
EXTERNAL_FORM = re.compile(
    r"docs\.google\.com/forms|forms\.gle|form\.run|formrun|formzu|form-mailer|"
    r"tayori\.com|hsforms|forms\.office\.com|mktoweb|pardot|jotform|typeform|"
    r"kintoneapp|questant|secure\.shop-pro\.jp/\?mode=inq|ssl\.form-mailer|f\.msgs\.jp|talkappi", re.I)
OTHER_PURPOSE = re.compile(r"出店|テナント|出展|広告媒体|広告掲載|媒体|採用|求人|openstore|tenant|recruit|advertis", re.I)
# 会社のホームページではないページの置き場(SNS・予約・掲載・口コミ・地図)。地図に登録のHP・検索で見つけたHP・ポータルの外部リンクの
# 3か所で同じ表を使う(2026-10-21 破壊レポート 致命傷3: 地図のHP欄が楽天トラベル・じゃらん等だと、施設の電話が載るので照合が通り「送れる」になっていた)。
# ホスト名の末尾で照合する(x.com が livemax.com に当たらないように、区切りの「.」から)。増やすときは、会社が自分のHPを置かないドメインだけ入れる
SNS_HOSTS = ("youtube.com", "youtu.be", "x.com", "twitter.com", "instagram.com", "facebook.com", "tiktok.com", "line.me", "lin.ee",
             "ameblo.jp", "lit.link", "linktr.ee", "note.com", "threads.net")
PORTAL_HOSTS = (
    # 美容・サロンの予約・掲載
    "nailbook.jp", "hotpepper.jp", "b-merit.jp", "tol-app.jp", "hpr.jp", "minimo.jp", "minimodel.jp", "rekubi.com", "epark.jp", "ekiten.jp",
    "salonboard.com", "ozmall.co.jp", "reserva.be", "airrsv.net", "coubic.com",
    # 宿・旅行の予約
    "rakuten.co.jp", "jalan.net", "ikyu.com", "booking.com", "expedia.co.jp", "expedia.com", "hotels.com", "agoda.com", "trip.com",
    "tripadvisor.jp", "tripadvisor.com", "relux.jp", "yadoplaza.com", "jtb.co.jp", "knt.co.jp", "travel.yahoo.co.jp",
    # 飲食の掲載
    "tabelog.com", "gnavi.co.jp", "retty.me", "hitosara.com",
    # 住宅・不動産の掲載
    "suumo.jp", "homes.co.jp", "athome.co.jp", "chintai.net",
    # ブライダルの掲載
    "zexy.net", "mwed.jp", "weddingpark.net", "hanayume.com",
    # 地図・電話帳・口コミ
    "itp.ne.jp", "mapion.co.jp", "navitime.co.jp", "loco.yahoo.co.jp",
)
SOCIAL = re.compile(r"(^|\.)(" + "|".join(re.escape(h) for h in SNS_HOSTS + PORTAL_HOSTS) + r")$", re.I)


def is_not_company_site(url_or_host):
    """SNS・予約・掲載・口コミのページか(その会社のホームページとして扱わない)"""
    u = str(url_or_host or "")
    host = urlparse(u if "//" in u else "//" + u).netloc.split(":")[0]
    return bool(host and SOCIAL.search(host))
BAD_MAIL = re.compile(r"privacy|recruit|saiyo|jinji|\bir@|example|sentry|wixpress|@.*\.(png|jpg|gif|webp)$", re.I)
REFUSE = re.compile(
    r"(営業(?:目的|電話|メール|活動|行為|の?ご?連絡|の?お?問い?合わせ|のお?電話)|セールス|勧誘|売り?込み|提案目的|宣伝目的|広告目的|sales(?:inquir|call|email|solicit))[^。\n]{0,30}\n?[^。\n]{0,30}"
    r"(お断り|ご遠慮|遠慮願|お控えください|禁止|おやめ|不要です|受け?付け?(し)?て(い|お)りません|受け?付け?(いた)?しません|"
    r"お受け(して)?(い|お)りません|お受けできません|受け付けできません|"
    r"対応(いた)?しかね|対応して(い|お)りません|返信(いた)?しかね|返答(いた)?しかね|回答(いた)?しかね|"
    r"ご?返信(は)?(いた)?しません|ご?返答(いた)?しません|お答えできません|notaccept)"
    r"|(お断り|ご遠慮)[^。\n]{0,15}(営業(?!時間|日|中|所|開始|終了)|勧誘|セールス|売り?込み)", re.I)


def refuse_in(text):
    """お断り文言を探す。空白(スペース・タブ)は除くが改行は残し、別の行どうしをつないで誤検出しないようにする。
    陰性対照(お断りではないので検出してはいけない): 営業時間の案内・提案書のご提出・送信内容の控え・画像の無断転載禁止・広告掲載のご相談"""
    return REFUSE.search(re.sub(r"[ \t\u3000]+", "", text))


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SKIP_LINK = re.compile(r"予約|reserv|booking|member|login|password|resetp|regist|会員登録|ログイン|mente|maintenance|落とし物|忘れ物|lost|_tel|faq|よくある", re.I)
SKIP_LINK_HARD = re.compile(r"member|login|password|resetp|regist|会員登録|ログイン|mente|maintenance|落とし物|忘れ物|lost|_tel|faq|よくある", re.I)
SKIP_LINK_SOFT = re.compile(r"予約|reserv|booking", re.I)  # 「ご予約・お問い合わせ」のように問い合わせの語も並ぶリンクは捨てない
FALLBACK_PATHS = (
    "/contact/", "/contact.html", "/contact-us/", "/inquiry/", "/inquiry.html",
    "/mailform/", "/mail-form/", "/form/", "/otoiawase/",
)
EXTRA_CONTACT_WORDS = re.compile(r"contact-us|mailform|mail-form|otoiawase", re.I)
MAX_CONTACT_PAGES = 5
HUB_LINKS_PER_PAGE = 2


MAX_BYTES = 3_000_000  # 1ページで読む上限。フッターの電話・住所まで届く大きさで、巨大な応答でメモリを使い切らない
MAX_HOPS = 5
MAX_SECONDS = 30  # 1ページ(または robots.txt)の読み込み全体の上限。1秒に1バイトずつ送る相手で、いつまでも止まらないように
ROBOTS_MAX_BYTES = 500_000  # Google の robots.txt の解釈上限(500 KiB)と同じ


def _read_limited(r, limit):
    """応答の本文を、容量 limit と時間 MAX_SECONDS の両方で打ち切って読む(読み終わったバイト列を返す)。
    1回の受信待ちは TIMEOUT 秒で切れるが、少しずつ送られ続けると終わらないので、受信のたびに全体の時間を見る"""
    t0 = time.monotonic()
    chunks, n = [], 0
    raw = r.raw
    while n < limit and time.monotonic() - t0 < MAX_SECONDS:
        try:
            ch = raw.read1(65536, decode_content=True)
        except AttributeError:
            ch = raw.read(1024, decode_content=True)
        if not ch:
            break
        chunks.append(ch)
        n += len(ch)
    return b"".join(chunks)[:limit]


class Site:
    """1つのサイトを礼儀正しく読む。robots.txt はホスト(origin)ごとに見る。転送は1回ずつ自分で追い、転送先ごとに
    公開アドレスか(netguard)と robots.txt を先に確かめてから取る。取得できなかった理由は、許可と取り違えない"""

    def __init__(self, base_url, budget=None, line_id=None):
        self.base = base_url
        self.budget = budget
        self.line_id = line_id or base_url
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.5"})
        self.last = 0.0
        self.robots = {}  # origin -> RobotFileParser / "unknown" / "disallow"

    def _claim(self, url, kind):
        if self.budget:
            self.budget.claim(self.line_id)

    def _wait(self):
        d = time.time() - self.last
        if d < INTERVAL:
            time.sleep(INTERVAL - d)
        self.last = time.time()

    def _robots_for(self, url):
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin in self.robots:
            return self.robots[origin]
        res = "unknown"
        rurl = origin + "/robots.txt"
        try:
            for _ in range(5):  # www への転送のあと https への転送、のように2〜3回続く例がある
                ok, _why = netguard.check_url(rurl)
                if not ok:
                    break
                self._wait()
                self._claim(rurl, "robots")
                r = self.s.get(rurl, timeout=TIMEOUT, allow_redirects=False, stream=True)
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    rurl = urljoin(rurl, r.headers["Location"])
                    r.close()
                    continue
                if r.status_code == 200:
                    raw = _read_limited(r, ROBOTS_MAX_BYTES)  # robots.txt は小さい。巨大な応答・遅い応答で止まらないよう、容量と時間に上限を置く
                    r.close()
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(raw.decode(r.encoding or "utf-8", errors="replace").splitlines())
                    res = rp
                elif r.status_code in (401, 403):
                    res = "disallow"  # 規則を見せない=立入禁止と読む(Python の標準の扱いと同じ)
                elif r.status_code in (404, 410) or (400 <= r.status_code < 500 and r.status_code != 429):
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse([])
                    res = rp  # robots.txt が無い=制限なし
                else:
                    res = "unknown"  # 429・5xx は確認できなかった。許可とは読まない
                break
        except requests.RequestException:
            res = "unknown"
        self.robots[origin] = res
        return res

    def allowed(self, url):
        r = self._robots_for(url)
        return isinstance(r, urllib.robotparser.RobotFileParser) and r.can_fetch(UA, url)

    def get(self, url, url_allowed=None):
        """Returns (final_url, html) or (None, reason)."""
        for _hop in range(MAX_HOPS + 1):
            if url_allowed and not url_allowed(url):
                return None, "blocked: URL policy"
            ok, why = netguard.check_url(url)
            if not ok:
                return None, "blocked: " + why
            rb = self._robots_for(url)
            if rb == "unknown":
                return None, "robots確認不能"
            if rb == "disallow" or not rb.can_fetch(UA, url):
                return None, "robots"
            self._wait()
            try:
                self._claim(url, "page")
                r = self.s.get(url, timeout=TIMEOUT, allow_redirects=False, stream=True)
            except requests.RequestException as e:
                return None, f"error: {type(e).__name__}"
            try:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                if r.status_code >= 400:
                    return None, f"HTTP {r.status_code}"
                if "html" not in r.headers.get("Content-Type", "html"):
                    return None, "not html"
                r._content = _read_limited(r, MAX_BYTES)  # 容量か時間の上限で打ち切る。先頭〜上限までで判定する
                r._content_consumed = True
            except requests.RequestException as e:
                return None, f"error: {type(e).__name__}"
            finally:
                r.close()
            r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
            return url, r.text
        return None, "転送が多すぎる"


NOT_INQUIRY = re.compile(r"search|newsletter|mailmag|mail-?magazine|メルマガ|subscribe|login|signin|member|cart|comment|"
                         r"recruit|apply|review|survey|enquete|myasp|acmailer|list-manage|benchmark", re.I)
# 予約・エントリーの印。フォームの文言に問い合わせ・相談の語があれば、問い合わせフォームとして採る(「ご予約・ご相談はこちら」・class=entry-form)
NOT_INQUIRY_SOFT = re.compile(r"reserv|booking|yoyaku|予約|entry", re.I)
NOT_INQUIRY_TEXT = re.compile(r"予約|応募|エントリー|アンケート|口コミ|メルマガ|メールマガジン|ログイン|会員登録|資料請求")
INQUIRY_TEXT = re.compile(r"お問い?合わ?せ|問合せ|問い合わせ|ご?相談|ご?質問|contact|inquiry|toiawase", re.I)
INQUIRY_FIELDS = re.compile(r"subject|件名|message|content|body|用件|内容|inquiry|toiawase", re.I)  # 氏名・電話だけではメルマガ登録や申込と区別できない


def _is_inquiry_form(f):
    """問い合わせフォームか。検索窓・ログイン・メルマガ登録は除く。本文欄(textarea)があるか、メール欄に氏名・用件などの欄が添う形"""
    attrs = " ".join([f.get("id") or "", " ".join(f.get("class") or []), f.get("action") or "", f.get("name") or ""])
    if NOT_INQUIRY.search(attrs):
        return False
    shown = f.get_text(" ", strip=True)[:400]
    asks = INQUIRY_TEXT.search(shown)
    if NOT_INQUIRY_SOFT.search(attrs) and not asks:
        return False  # 予約・エントリーの印があり、問い合わせ・相談の語が無いフォーム
    if NOT_INQUIRY_TEXT.search(shown) and not asks:
        return False  # 予約・応募・アンケートなどの文言があり、問い合わせ・相談の語が無いフォーム
    inputs = f.find_all("input")
    if any((i.get("type") or "").lower() in ("password", "search") for i in inputs):
        return False
    if f.find("textarea"):
        return True
    names = " ".join((i.get("name") or "") + (i.get("id") or "") + (i.get("placeholder") or "") for i in inputs + f.find_all("select"))
    has_mail = any((i.get("type") or "").lower() == "email" for i in inputs) or re.search(r"mail|メール", names, re.I)
    return bool(has_mail and INQUIRY_FIELDS.search(names))


def titles_of(soup):
    """ページの題名・見出し・サイト名。会社名が出ているかの照合に使う"""
    parts = [soup.title.get_text(" ", strip=True) if soup.title else ""]
    parts += [h.get_text(" ", strip=True) for h in soup.find_all("h1")[:3]]
    for m in soup.find_all("meta"):
        if (m.get("property") or m.get("name") or "").lower() in ("og:site_name", "og:title", "application-name"):
            parts.append(m.get("content") or "")
    return " ".join(parts)


def page_form_state(soup):
    """'form' / 'widget' / None for one page."""
    for f in soup.find_all("form"):
        if _is_inquiry_form(f):
            return "form"
    ext_widget = False
    for tag in soup.find_all(["iframe", "script", "a"]):
        src = tag.get("src") or tag.get("href") or ""
        if not EXTERNAL_FORM.search(src):
            continue
        if OTHER_PURPOSE.search(src) or re.search(r"cart|申込|申し込み|アンケート|予約|応募|entry|survey|subscribe", src, re.I):
            continue  # 申込・予約・カート・アンケートの外部フォームは、問い合わせフォームとして数えない
        if tag.name == "a" and not STRONG_WORDS.search(tag.get_text(" ", strip=True)):
            continue  # リンクの文字に「お問い合わせ」が無いもの(Powered by など)は数えない
        if tag.name in ("iframe", "a"):
            return {"kind": "external_form", "url": src}
        ext_widget = True
    return "widget" if ext_widget else None


def looks_script_rendered(soup):
    text = soup.get_text(" ", strip=True)
    return len(text) < 200 and len(soup.find_all("script")) >= 3


def contact_links(soup, page_url):
    host = urlparse(page_url).netloc
    scored = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        label = a.get_text(" ", strip=True) + " " + (a.get("title") or "") + " " + " ".join(
            i.get("alt", "") for i in a.find_all("img"))
        if SKIP_LINK_HARD.search(label + " " + href):
            continue
        if SKIP_LINK_SOFT.search(label + " " + href) and not STRONG_WORDS.search(label + " " + href):
            continue  # 予約だけのリンク。「ご予約・お問い合わせ」は採る
        hit_label = STRONG_WORDS.search(label)
        hit_href = CONTACT_WORDS.search(href) or EXTRA_CONTACT_WORDS.search(href)
        if not (hit_label or hit_href):
            continue
        url = urljoin(page_url, href).split("#")[0]
        if EXTERNAL_FORM.search(url) and not hit_label:
            continue  # アドレスに form とあるだけの外部フォーム(イベント申込・アンケート)は、問い合わせとして採らない
        other = urlparse(url).netloc
        if other != host and not EXTERNAL_FORM.search(url) and _root(other) != _root(host):
            continue  # 別の組織のドメインは採らない(別会社のフォームを、この会社のフォームと取り違えるため)
        if re.search(r"採用|recruit|求人|エントリー|entry|資料請求", label + href, re.I):
            score = 1
        else:
            score = (3 if hit_label else 0) + (2 if hit_href else 0)
        scored.append((score, url))
    seen, out = set(), []
    for _, u in sorted(scored, key=lambda x: -x[0]):
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


SHARED_HOSTS = ("jimdofree.com", "jimdo.com", "wixsite.com", "amebaownd.com", "fc2.com", "sakura.ne.jp", "xsrv.jp", "tokyo.jp",
                "wordpress.com", "blogspot.com", "hatenablog.com", "shopinfo.jp", "stores.jp", "thebase.in", "crayonsite.net")  # 別々の組織が同じ末尾を使う


def _root(netloc):
    host = netloc.lower().split(":")[0]
    if any(host == s or host.endswith("." + s) for s in SHARED_HOSTS):
        return host  # 共用の末尾は、ホスト名全体を1つの組織として扱う
    parts = host.split(".")
    n = 3 if len(parts) >= 3 and parts[-2] in ("co", "or", "ne", "ac", "go", "ed", "lg") else 2
    return ".".join(parts[-n:])


def _mail(soup, text):
    cands = [a["href"][7:].split("?")[0].strip() for a in soup.find_all("a", href=True) if a["href"].lower().startswith("mailto:")]
    cands = [m for m in cands if EMAIL.fullmatch(m)]  # mailto:address のようなひな形の値を採らない
    cands += EMAIL.findall(text)
    return next((m for m in cands if not BAD_MAIL.search(m)), "")


def overview_of(soup):
    """トップページのタイトルと説明文(meta description)。文面の書き出しの根拠にする。本文は読まない"""
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    m = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    desc = (m.get("content") or "").strip() if m else ""
    return re.sub(r"\s+", " ", f"{title} / {desc}" if desc else title)[:160]


# LINE公式の友だち追加URL(共有ボタンの line.me/R/msg は含めない)。自動送信はしない。人が友だち追加して送るための手がかり
LINE_ADD = re.compile(r"https?://(?:lin\.ee/[\w\-./]+|line\.me/(?:R/)?ti/p/[^\s\"'<>)]+|page\.line\.me/[^\s\"'<>)]+)", re.I)


def find_line_url(html):
    m = LINE_ADD.search(html or "")
    return m.group(0).rstrip(".,;") if m else ""


CHECK_RULES = "r5"  # フォーム判定・照合・URL欄の規則を変えたら必ず上げる。古い規則で出た判定を保存結果から再利用しない(行の指紋に含める)


RECRUIT_TITLE = re.compile(r"求人|採用|募集|リクルート|recruit|career", re.I)  # 迷う題名は要目視へ(安全側)

ABOUT_LINK = re.compile(r"会社概要|店舗情報|店舗案内|サロン情報|サロン概要|アクセス|運営会社|about|salon|shop|company|access", re.I)


def about_text(soup, final, site, skip):
    """トップページから会社概要・アクセスなどのリンクを1つだけ開いて本文を返す(同じ組織のドメインだけ)。無ければ空"""
    host = urlparse(final).netloc
    tries = 0
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        label = a.get_text(" ", strip=True) + " " + href
        if not ABOUT_LINK.search(label) or SKIP_LINK.search(label):
            continue
        u = urljoin(final, href).split("#")[0]
        if u in skip or _root(urlparse(u).netloc) != _root(host) or urlparse(u).netloc != host:
            continue  # 同じホストだけ(robots.txt を取ったホストと同じ)
        tries += 1
        if tries > 2:
            return ""  # 取得に失敗しても、試すのは2つまで
        f, h = site.get(u)
        if f is not None:
            return BeautifulSoup(h, "html.parser").get_text("\n", strip=True)
    return ""


def external_form_allowed(url):
    ok, why = netguard.check_url(url)
    if not ok:
        return False, "blocked: " + why
    checker = Site(url)
    rb = checker._robots_for(url)
    if rb == "unknown":
        return False, "robots確認不能"
    if rb == "disallow" or not checker.allowed(url):
        return False, "robots"
    return True, ""


def set_external_form_result(res, form_url):
    ok, why = external_form_allowed(form_url)
    if not ok:
        res.update(verdict="取得失敗" if why != "robots" else "robots禁止", form_url=form_url, note=why)
        return False
    if OTHER_PURPOSE.search(form_url) or re.search(r"申込|申し込み|アンケート|予約|応募|entry|survey", form_url, re.I):
        res.update(verdict="要目視", form_url=form_url, note="外部フォームだが、問い合わせ以外の用途の可能性")
    else:
        res.update(verdict="フォームあり", form_url=form_url, note="外部フォームサービス")
    return True


def check(url, max_pages=MAX_CONTACT_PAGES, row=None, budget=None, line_id=None):
    """Returns dict: verdict, form_url, email, refuse, refuse_text, note, overview, ev, ev_why。
    row(名前・電話・住所を持つ辞書)を渡すと、このHPがその会社のものかを hp_verify で照合して ev・ev_why に入れる"""
    res = {"verdict": "見つからず", "form_url": "", "email": "", "refuse": False, "refuse_text": "", "note": "", "overview": "", "ev": "", "ev_why": "", "line_url": ""}
    if not url.startswith("http"):
        url = "http://" + url
    if is_not_company_site(url):
        res["needs_official_search"] = True
        res.update(verdict="見つからず", note="登録URLがSNS・予約サイト・掲載サイトのページ（会社のホームページではない）")
        return res
    site = Site(url, budget=budget, line_id=line_id)
    final, html = site.get(url)
    if final is None and html.startswith("HTTP 404") and urlparse(url).path not in ("", "/"):
        p = urlparse(url)
        final, html = site.get(f"{p.scheme}://{p.netloc}/")
    if final is None:
        res["verdict"] = "robots禁止" if html == "robots" else "取得失敗"
        res["note"] = {"HTTP 403": "サイトが自動取得を拒否", "HTTP 404": "登録URLが古い(ページなし)"}.get(html, html)
        if row is not None:
            res["verify_detail"] = {"level": "取得不可", "why": res["note"], "category": "fetch_failed"}
        return res
    soup = BeautifulSoup(html, "html.parser")
    res["overview"] = overview_of(soup)
    res["line_url"] = find_line_url(html)
    texts = [soup.get_text("\n", strip=True)]
    candidates = contact_links(soup, final)
    top_state = page_form_state(soup)
    if isinstance(top_state, dict) and top_state.get("kind") == "external_form":
        top_external_form = urljoin(final, top_state["url"])
        widget_seen = False
    else:
        top_external_form = ""
        widget_seen = top_state == "widget"
    mail_found = _mail(soup, texts[0])
    contact_reached = False
    remaining_pages = max(0, max_pages - 1)
    queue = (candidates or [urljoin(final, p) for p in FALLBACK_PATHS])[:remaining_pages]
    for cu in queue:
        if EXTERNAL_FORM.search(cu):
            ok, why = external_form_allowed(cu)
            if not ok:
                res.update(verdict="取得失敗" if why != "robots" else "robots禁止", form_url=cu, note=why)
                break
            if OTHER_PURPOSE.search(cu) or re.search(r"申込|申し込み|アンケート|予約|応募|entry|survey", cu, re.I):
                res.update(verdict="要目視", form_url=cu, note="外部フォームだが、問い合わせ以外の用途の可能性")
            else:
                res.update(verdict="フォームあり", form_url=cu, note="外部フォームサービス")
            break
        cf, chtml = site.get(cu)
        if cf is None:
            continue
        cs = BeautifulSoup(chtml, "html.parser")
        res["line_url"] = res["line_url"] or find_line_url(chtml)
        texts.append(cs.get_text("\n", strip=True))
        contact_reached = True
        st = page_form_state(cs)
        if isinstance(st, dict) and st.get("kind") == "external_form":
            set_external_form_result(res, urljoin(cf, st["url"]))
            break
        if st == "form":
            head = " ".join(t.get_text(" ", strip=True) for t in cs.find_all(["title", "h1", "h2"])[:4])
            if OTHER_PURPOSE.search(cf + " " + head):
                res.update(verdict="要目視", form_url=cf, note="出店・広告・採用など別の用途のフォームの可能性")
            else:
                res.update(verdict="フォームあり", form_url=cf)
            break
        if st == "widget" or looks_script_rendered(cs):
            widget_seen = True
            res["form_url"] = res["form_url"] or cf
        mail_found = _mail(cs, texts[-1]) or mail_found
        res["form_url"] = res["form_url"] or cf
        # contact hub page that links to the actual form: follow only a few same-site candidates.
        if len(queue) < remaining_pages:
            queue.extend([u for u in contact_links(cs, cf) if u != cf and u not in queue][:max(0, min(HUB_LINKS_PER_PAGE, remaining_pages - len(queue)))])
    else:
        if top_external_form:
            set_external_form_result(res, top_external_form)
        elif top_state == "form" and not candidates:
            res.update(verdict="フォームあり", form_url=final, note="トップページにフォーム")
        elif widget_seen:
            res.update(verdict="要目視", note="画面の描画に頼るフォームの可能性")
        elif mail_found:
            res.update(verdict="メールのみ", email=mail_found)
        elif looks_script_rendered(soup):
            res.update(verdict="要目視", note="トップページが描画頼み")
        elif contact_reached:
            res.update(verdict="要目視", note="問い合わせページはあるがフォームを確認できず（電話のみ・描画頼みの可能性）")
    if res["verdict"] in ("フォームあり", "メールのみ") and RECRUIT_TITLE.search(soup.title.get_text(" ", strip=True) if soup.title else ""):
        res.update(verdict="要目視", note="トップページの題名が求人・採用のページ。営業の送り先として確認が要る")
    if row is not None:
        detail = hp_verify.judge_detail(row, "\n".join(texts), titles_of(soup))
        detail["candidate_url"] = final
        ev, why = detail["level"], detail["why"]
        if ev not in hp_verify.STRONG and (row.get("tel") or row.get("address")):
            more = about_text(soup, final, site, set(queue))
            if more:
                texts.append(more)
                detail = hp_verify.judge_detail(row, "\n".join(texts), titles_of(soup))
                detail["candidate_url"] = final
                ev, why = detail["level"], detail["why"]
        if ev in hp_verify.STRONG and not __import__("hp_finder").same_company(row.get("name", ""), titles_of(soup), row.get("area", "")):
            detail = hp_verify.title_mismatch_detail(detail, "電話・住所は載るが、サイト名が会社名と合わない（ニュース・予約・施設のページの疑い）")
            ev, why = detail["level"], detail["why"]
        res["ev"], res["ev_why"] = ev, why
        res["verify_detail"] = detail
    fu = urlparse(res["form_url"])
    if res["form_url"] and res["verdict"] != "フォームあり" and fu.path in ("", "/") and not fu.query:
        # 問い合わせページのつもりで開いたURLがトップページ(リンク切れ・トップへの転送)。欄にトップページを入れると取得できたように見えるので空にする
        res["form_url"] = ""
        res["note"] = (res["note"] + " / " if res["note"] else "") + "問い合わせページのURLを取得できていない(トップページに戻された)"
    for t in texts:
        m = refuse_in(t)
        if m:
            res["refuse"] = True
            res["refuse_text"] = m.group(0)[:60]
            break
    return res
