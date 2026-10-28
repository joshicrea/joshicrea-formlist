"""ポータルサイトなどのURLから、掲載されている会社のホームページを集める。実行環境: Windows / クロスプラットフォーム
使い方は formlist.py --portal <URL>。リストの用意が要らない。GETのみ・robots.txt順守・1ページ1.2秒。
手順: (1) 一覧ページにある外部サイトへのリンクを会社HPとして拾う
      (2) 一覧の「次へ」をたどって、複数ページから拾う
      (3) 一覧と同じ階層の個別ページも上限の範囲内で開き、その中の外部リンクを拾う
上限(limit)は「候補を集める数」。会社HPが個別ページにしか載っていないポータルでも取りこぼさないよう、
個別ページは一覧の取れ具合にかかわらず上限まで開く(開いた件数はログに出す)"""
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import site_checker as sc

SKIP_HOSTS = ("google.", "apple.com", "play.google", "amazon.", "wikipedia.", "note.com", "prtimes.jp", "github.com")

MAX_PAGES = 10  # 一覧ページをたどる上限(「次へ」の暴走防止)

_SITES = {}


def _get(url):
    """site_checker.Site と同じ関門(公開アドレスだけ・転送先ごとのrobots確認・容量上限)を通して読む"""
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    site = _SITES.setdefault(origin, sc.Site(origin))
    final, html = site.get(url)
    return BeautifulSoup(html, "html.parser") if final else None


def _external(soup, page_url):
    host = urlparse(page_url).netloc.lower()
    out = []
    for a in soup.find_all("a", href=True):
        u = urljoin(page_url, a["href"])
        p = urlparse(u)
        h = p.netloc.lower()
        if p.scheme not in ("http", "https") or not h or h == host or h.endswith("." + host):
            continue
        if sc.SOCIAL.search(h) or any(s in h for s in SKIP_HOSTS):
            continue
        out.append((a.get_text(" ", strip=True)[:40] or h, f"{p.scheme}://{h}/"))
    return out


def _next_page(soup, page_url):
    """一覧の「次へ」らしいリンクを1つ返す(rel=next・文言・aria-label で判定。同じサイト内だけ)"""
    host = urlparse(page_url).netloc.lower()
    for a in soup.find_all("a", href=True):
        u = urljoin(page_url, a["href"]).split("#")[0]
        if urlparse(u).netloc.lower() != host or u == page_url:
            continue
        rel = " ".join(a.get("rel") or [])
        text = a.get_text(" ", strip=True)
        label = (a.get("aria-label") or "").strip()
        if "next" in rel or text in ("次へ", "次のページ", "次ページ", "›", "»") or "次" in label:
            return u
    return None


def fetch(portal_url, limit, log):
    limit = max(1, int(limit or 1))
    pages, seen_pages = [], set()
    url = portal_url
    while url and url not in seen_pages and len(pages) < MAX_PAGES:
        seen_pages.add(url)
        soup = _get(url)
        if soup is None:
            if not pages:
                raise RuntimeError("ポータルのページを開けませんでした（robots.txtで禁止、または接続できません）")
            log(f"  一覧の{len(pages) + 1}ページ目を開けなかったので、取れたところまでで集めます")
            break
        pages.append((url, soup))
        url = _next_page(soup, url)
    if len(pages) > 1:
        log(f"  一覧は{len(pages)}ページたどりました")

    found, hosts = [], set()

    def take(pairs):
        for n, u in pairs:
            h = urlparse(u).netloc.lower()
            if h and h not in hosts:
                hosts.add(h)
                found.append((n, u))

    for u, soup in pages:
        take(_external(soup, u))

    seg = urlparse(portal_url).path.strip("/").split("/")[0]
    inner, seen = [], set()
    for u, soup in pages:
        host = urlparse(u).netloc.lower()
        for a in soup.find_all("a", href=True):
            iu = urljoin(u, a["href"]).split("#")[0]
            p = urlparse(iu)
            if p.netloc.lower() == host and p.path.strip("/") and p.path.strip("/").split("/")[0] == seg and iu not in seen and iu not in seen_pages:
                seen.add(iu)
                inner.append(iu)
    opened = 0
    for iu in inner:
        if opened >= limit or len(found) >= limit:
            break
        s = _get(iu)
        opened += 1
        if s is not None:
            take(_external(s, iu))
    if inner:
        log(f"  個別ページは{len(inner)}件中{opened}件を開きました(候補{len(found)}件)")
    if not found:
        log("  ポータルから会社のホームページが1件も見つかりませんでした(一覧の作りが想定と違うか、個別ページの先にあります)")
    return [{"name": n, "industry": "", "area": "", "url": u, "source": "ポータル: " + portal_url, "osm_ref": ""} for n, u in found[:limit]]
