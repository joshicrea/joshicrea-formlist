"""ポータルサイトなどのURLから、掲載されている会社のホームページを集める。実行環境: Windows / クロスプラットフォーム
使い方は formlist.py --portal <URL>。リストの用意が要らない。GETのみ・robots.txt順守・1ページ1.2秒。
手順: (1) そのページにある外部サイトへのリンクを会社HPとして拾う
      (2) 足りなければ、同じサイト内で同じ階層（例 /companies/）の個別ページを開き、その中の外部リンクを拾う"""
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import site_checker as sc

SKIP_HOSTS = ("google.", "apple.com", "play.google", "amazon.", "wikipedia.", "note.com", "prtimes.jp", "github.com")


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


def fetch(portal_url, limit, log):
    soup = _get(portal_url)
    if soup is None:
        raise RuntimeError("ポータルのページを開けませんでした（robots.txtで禁止、または接続できません）")
    found = _external(soup, portal_url)
    if len(found) < 5:
        pu = urlparse(portal_url)
        seg = pu.path.strip("/").split("/")[0]
        inner, seen = [], set()
        for a in soup.find_all("a", href=True):
            u = urljoin(portal_url, a["href"]).split("#")[0]
            p = urlparse(u)
            if p.netloc == pu.netloc and p.path.strip("/").startswith(seg) and p.path.strip("/") != pu.path.strip("/") and u not in seen:
                seen.add(u)
                inner.append(u)
        log(f"  一覧のページから個別ページ{len(inner)}件をたどります（最大{limit}件）")
        for u in inner[:limit]:
            s = _get(u)
            if s is not None:
                found += _external(s, u)[:1]
    return [{"name": n, "industry": "", "area": "", "url": u, "source": "ポータル: " + portal_url, "osm_ref": ""} for n, u in found]
