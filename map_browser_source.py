"""Company list source: Google Maps を自動ブラウザ操作で検索して集める。

Runtime: Windows / cross-platform, Python 3.9+, playwright (Chromium).
Source interface (osm_source と同じ):
    fetch(source, area_name, term, log, limit) -> list[dict]
    each dict: name, industry, area, url, source, osm_ref
source は "google"。Yahoo!マップは2026-10-10に外した。

注意:
- Google マップは robots.txt と利用規約が自動取得を認めていない。1件ごとに間隔を空けて、検索1回あたり limit 件までしか見ない
  (limit は呼び出し側の「調べる会社数の上限」がそのまま渡る。formlist からは50〜200。町名に分ける地域では、町名の数だけ検索する)。
  ブロックされたら止まる(回避はしない)。
- 取りこぼし(読めなかった店・上限の件数に届く前に一覧が止まった検索・結果1件で施設ページに直行した検索)は RUN_COUNTS に数え、
  formlist が探索の内訳に出す。
- Yahoo!マップは外した。map.yahoo.co.jp/robots.txt が /proxy を Disallow しており、検索結果の元になる通信 /proxy/search はそこに当たる
  (2026-10-10 に robots.txt を取得して確認)。以前のこの説明「robots.txt 上は許可」は誤りだった。
"""
import re
import time
import unicodedata
import datetime as dt
from threading import Lock
from urllib.parse import quote, unquote, urlsplit

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
GL_ARGS = ["--use-gl=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]  # 地図描画(WebGL)が無い環境でも結果欄が出るように
PAUSE = 1.5
DETAIL_SETTLE_MS = 800  # 詳細欄の見出しと項目が出たあと、電話・住所・ホームページの欄が揃うまで待つ時間
BLOCK_HEAVY = False  # True で、画像・動画・フォントの読み込みを止める(地図のタイルや写真は、読むのに要らない)
MAX_SCROLL_ATTEMPTS = 30

# この回(1プロセス)の地図の検索で、取れた数と取りこぼした数。並列で取るので足すときは鍵を取る
RUN_COUNTS = {}
RUN_WARNINGS = []  # やり直しても半数以上読めなかった検索語の警告文。formlist が内訳JSON・ログの最後に出す
_COUNT_LOCK = Lock()


def tally(**kw):
    with _COUNT_LOCK:
        for k, v in kw.items():
            RUN_COUNTS[k] = RUN_COUNTS.get(k, 0) + v


# 施設の詳細欄から、見出し・公式サイト・電話・住所を読む(一覧のカードを開いたときと、施設ページに直行したときで共用)
DETAIL_JS = r"""() => {
  const g = s => [...document.querySelectorAll(s)];
  const web = g("a[data-item-id='authority']")[0];
  const lab = id => { const e = g("[data-item-id^='" + id + "']")[0]; return e ? (e.getAttribute('aria-label') || e.innerText || '').replace(/^[^:：]*[:：]\s*/, '').trim() : ''; };
  return {names: g('h1').map(e => e.innerText).map(x => x.replace(/^\s*(スポンサー|広告)\s*/, '').trim()).filter(x => x && x !== '結果'),
          name: '',
          url: web ? web.href : '', href: location.href, tel: lab('phone'), address: lab('address')};
}"""


def _row(name, term, area, url, source, ref, tel="", address=""):
    fetched_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S") if (tel or address) else ""
    return {"name": name, "industry": term, "area": area, "url": url, "source": source, "osm_ref": ref,
            "tel": tel, "address": address,
            "comparison_phone": tel, "comparison_address": address,
            "comparison_source": source if (tel or address) else "",
            "comparison_source_url": ref if (tel or address) else "",
            "comparison_fetched_at": fetched_at}


def _browser(p):
    b = p.chromium.launch(headless=True, args=GL_ARGS)
    ctx = b.new_context(locale="ja-JP", viewport={"width": 1400, "height": 1000}, user_agent=UA)
    if BLOCK_HEAVY:
        ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "media", "font") else route.continue_())
    return b, ctx


def _count_cards_with_scroll(pg, cards, limit):
    def result(count, status, attempts, reason="", error=""):
        return {
            "count": max(0, int(count or 0)),
            "status": status,
            "attempts": attempts,
            "reason": reason,
            "error": error,
        }

    def safe_count(known_count, attempts):
        try:
            return int(cards.count()), None
        except Exception as e:
            return known_count, result(
                known_count,
                "error",
                attempts,
                reason="count_failed",
                error=repr(e)[:120],
            )

    count, failed = safe_count(0, 0)
    if failed:
        return failed
    if count >= limit:
        return result(count, "limit_reached", 0)

    stagnant = 0
    last_error = ""

    for attempt in range(1, MAX_SCROLL_ATTEMPTS + 1):
        before = count
        try:
            pg.evaluate("""() => {
              const feed = document.querySelector('[role="feed"]');
              if (feed) feed.scrollBy(0, feed.scrollHeight);
              else window.scrollBy(0, document.body.scrollHeight);
            }""")
        except Exception as e:
            last_error = repr(e)[:120]

        try:
            pg.wait_for_timeout(1200)
        except Exception as e:
            last_error = repr(e)[:120]

        after, failed = safe_count(before, attempt)
        if failed:
            return failed

        if after <= before and before:
            try:
                cards.nth(before - 1).scroll_into_view_if_needed(timeout=2000)
            except Exception as e:
                last_error = repr(e)[:120]
            try:
                pg.wait_for_timeout(1200)
            except Exception as e:
                last_error = repr(e)[:120]

            after, failed = safe_count(before, attempt)
            if failed:
                return failed

        count = after
        if count >= limit:
            return result(count, "limit_reached", attempt)

        if count > before:
            stagnant = 0
        else:
            stagnant += 1
            if stagnant >= 3:
                return result(
                    count,
                    "partial",
                    attempt,
                    reason="no_new_cards_after_scroll",
                    error=last_error,
                )

    return result(
        count,
        "partial",
        MAX_SCROLL_ATTEMPTS,
        reason="scroll_attempt_limit_reached",
        error=last_error,
    )


PRIVATE_USE = re.compile("[\ue000-\uf8ff]")


def pick_name(names, label):
    """見出し(h1)の候補から会社名を選ぶ。クリックしたカードの名前(label)と一致するものを優先し、
    なければアイコン用の私用領域文字(U+E000-F8FF)を除いて最初に文字が残るもの。選べなければ空(呼び出し側で「読めず」扱い)"""
    for n in names:
        if label.strip() and n.strip() == label.strip():
            return n.strip()
    for n in names:
        cleaned = PRIVATE_USE.sub("", n).strip()
        if cleaned:
            return cleaned
    return ""


PLACE_ID = re.compile(r"0x[0-9a-f]+:0x[0-9a-f]+", re.I)


def place_id_of(href):
    """GoogleマップのURLに入る施設の識別子(0x...:0x...)。無ければ空。同じ名前の別の施設を取り違えないために使う"""
    m = PLACE_ID.search(unquote(href or ""))  # %3A で書かれたURL・大文字の16進数も同じ識別子として読む
    return m.group(0).lower() if m else ""


def usable_name(name):
    """会社名として使えるか。空・記号だけ・アイコン文字だけは使えない(壊れた名前の行を、後の照合に流さない)"""
    s = unicodedata.normalize("NFKC", PRIVATE_USE.sub("", name or ""))  # 全角英字・ハングル等の正しい社名を捨てない
    return any(unicodedata.category(ch)[0] in "LN" for ch in s)


def row_key(info, pid):
    """重複判定のキー。施設の識別子があればそれ。無ければ 名前+公式サイト+住所(同じ名前・同じ公式サイトの別店舗を消さないため、住所を足す)"""
    return ("id", pid) if pid else ("name", info.get("name", ""), info.get("url", ""), info.get("address", ""))


def _err_text(e, pg):
    """例外の型・先頭の1行・そのときのページのパス。ブロック(/sorry/)と通常の欠損を後から区別できるようにする"""
    try:
        path = urlsplit(pg.url).path[:40]
    except Exception:
        path = ""
    first = (str(e).splitlines() or [""])[0][:120]
    return f"{type(e).__name__}: {first} @ {path}"


RETRY_BAD_RATE = 0.5  # 1つの検索語で、候補のこの割合以上が読めなかったら、ブラウザを開き直して1回だけやり直す
RETRY_MIN_CARDS = 4


class GoogleBlocked(RuntimeError):
    """Google に確認画面・回数制限で止められた。続けて叩かない"""


class TransientFetchError(RuntimeError):
    """この1検索だけの失敗(通信・一時的な不具合)。ほかの検索は続けてよい"""


def _classify_failed_page(pg):
    """結果の一覧が出なかった画面を見て、blocked(確認画面・回数制限)/ empty(結果0件)/ place(1件だけで施設ページへ直行)/ other に分ける"""
    try:
        url = pg.url or ""
        text = (pg.inner_text("body", timeout=3000) or "")[:3000]
    except Exception:
        return "other"
    if "/sorry/" in url or "unusual traffic" in text or "通常と異なるトラフィック" in text or "ロボットではありません" in text:
        return "blocked"
    if "/maps/place/" in url:
        return "place"  # 結果が1件だけのときは、一覧を経ずに施設ページへ移る
    if "一致する結果が見つかりませんでした" in text or "No results found" in text or "見つかりませんでした" in text:
        return "empty"
    return "other"


def _clean_name(info, label):
    """詳細欄の見出しから会社名を選び、広告枠の「スポンサー」の行を外す"""
    if info.get("names"):  # 見出しの候補が取れていれば、そこから選ぶ(無ければ従来の name をそのまま使う)
        info["name"] = pick_name(info["names"], label)
    info["name"] = " ".join(l.strip() for l in (info.get("name") or "").splitlines() if l.strip() and l.strip() not in ("スポンサー", "広告", "Sponsored", "Ad"))
    return info


def _read_place_page(pg, term, area_name, q, log):
    """結果が1件だけで施設ページに直行したとき、その施設を1行として読む。読めなければ None(取りこぼしとして数える)"""
    try:
        pg.wait_for_selector("[data-item-id]", timeout=12000)
        pg.wait_for_timeout(DETAIL_SETTLE_MS)
        info = _clean_name(pg.evaluate(DETAIL_JS), "")
    except Exception as e:
        log(f"  「{q}」は結果が1件だけで施設ページに直接移りましたが、読めませんでした（原文: {_err_text(e, pg)}）")
        return None
    if not usable_name(info.get("name", "")):
        return None
    row = _row(info["name"], term, area_name, info.get("url", ""), "Googleマップ", info.get("href", ""), info.get("tel", ""), info.get("address", ""))
    pid = place_id_of(info.get("href", ""))
    if pid:
        row["place_id"] = pid
    return row


def fetch_google(area_name, term, log, limit=20):
    """検索語1つぶんの取得。候補の半分以上が読めなかったときは(Googleマップが詳細欄を開かなくなる・一時的な失敗)、やり直す。
    やり直してもだめなら、丸ごと失敗したことを警告に出す(一覧が黙って小さくなるのを防ぐ)"""
    stats = {}
    rows = _fetch_google_once(area_name, term, log, limit, stats)
    n, bad = stats.get("n", 0), stats.get("skipped", 0)
    if n >= RETRY_MIN_CARDS and bad >= n * RETRY_BAD_RATE:
        log(f"  Googleマップ: 「{term}」は候補{n}件のうち{bad}件が読めませんでした。ブラウザを開き直して1回だけやり直します")
        time.sleep(5)
        stats2 = {}
        rows2 = _fetch_google_once(area_name, term, log, limit, stats2)
        if len(rows2) > len(rows):
            rows, stats = rows2, stats2
            n, bad = stats2.get("n", 0), stats2.get("skipped", 0)
        if n >= RETRY_MIN_CARDS and bad >= n * RETRY_BAD_RATE:
            log(f"  [警告] 「{term}」は、やり直しても候補{n}件のうち{bad}件が読めませんでした。この検索語の結果は少ないままです(Googleマップ側の一時的な制限の可能性)")
            with _COUNT_LOCK:
                RUN_WARNINGS.append(f"検索語「{term}」({area_name})は、やり直しても候補{n}件中{bad}件が読めませんでした。この検索語の店は一覧に少ししか入っていません。時間をおいて探し直してください")
    tally(searches=1, rows=len(rows), cards=stats.get("n", 0), unreadable=stats.get("skipped", 0), bad_name=stats.get("bad_name", 0),
          cut_short=stats.get("cut_short", 0), place_read=stats.get("place_read", 0), place_missed=stats.get("place_missed", 0),
          empty=stats.get("empty", 0))  # やり直したときは、使った方の回だけを数える
    return rows


def _fetch_google_once(area_name, term, log, limit=20, stats=None):
    from playwright.sync_api import sync_playwright
    q = f"{term} {area_name}"
    rows = []
    pw_cm = sync_playwright()
    p = pw_cm.__enter__()
    try:
        b = None
        try:
            b, ctx = _browser(p)
            pg = ctx.new_page()
            pg.set_default_timeout(25000)
            last = None
            for k in range(2):
                try:
                    pg.goto("https://www.google.com/maps/search/" + quote(q) + "?hl=ja", timeout=45000, wait_until="domcontentloaded")
                    pg.wait_for_selector("a.hfpxzc", timeout=25000)
                    break
                except Exception as e:
                    last = e
                    pg.wait_for_timeout(4000)
            else:
                kind = _classify_failed_page(pg)
                if kind == "blocked":
                    raise GoogleBlocked(f"Google マップに「普段と違う通信」として止められました（確認画面）。時間をおいて、同じ場所を連続で探さないでください: {last!r}")
                if kind == "place":
                    row = _read_place_page(pg, term, area_name, q, log)
                    if row:
                        if stats is not None:
                            stats.update(place_read=1)
                        log(f"  「{q}」は、結果が1件だけで施設ページに直接移りました。その1件を取りました")
                        return [row]
                    if stats is not None:
                        stats.update(place_missed=1)
                    log(f"  「{q}」は、結果が1件だけで施設ページに直接移り、その1件を読めませんでした（取りこぼし1件）。町名を広げて探し直すと取れることがあります")
                    return []
                if kind == "empty":
                    if stats is not None:
                        stats.update(empty=1)
                    log(f"  「{q}」は、Googleマップで結果が0件でした（失敗ではありません）")
                    return []
                raise TransientFetchError(f"Google マップが開けない・結果が出ない（通信の失敗か、一時的な不具合です）: {last!r}")
            cards = pg.locator("a.hfpxzc")
            card_scroll = _count_cards_with_scroll(pg, cards, limit)
            n = min(card_scroll["count"], limit)
            if card_scroll["status"] != "limit_reached":
                why = {"no_new_cards_after_scroll": "下まで送っても新しい店が出なくなった", "scroll_attempt_limit_reached": "送る回数の上限に達した",
                       "count_failed": "店の数を数えられなかった"}.get(card_scroll["reason"], card_scroll["reason"])
                log(f"  「{q}」は、上限{limit}件に届く前に一覧が{card_scroll['count']}件で止まりました（{why}。店がそれだけしか無い場合と、地図がそれ以上出さない場合があり、見分けられません。"
                    f"原文: status={card_scroll['status']} reason={card_scroll['reason']} attempts={card_scroll['attempts']}）")
            seen = set()
            skipped = dup = bad_name = 0
            error_samples = []
            for i in range(n):
                info = None
                last_err = ""
                label = ""
                pid = ""
                for attempt in range(2):
                    try:
                        label = cards.nth(i).get_attribute("aria-label") or ""
                        pid = place_id_of(cards.nth(i).get_attribute("href") or "")
                        cards.nth(i).click()
                        # 前の施設の詳細欄が残っているので、見出しがこの施設の名前に変わり、URLがこの施設の識別子になるまで待つ
                        pg.wait_for_function("a => [...document.querySelectorAll('h1')].some(e => e.innerText.trim() === a.l) && (!a.p || decodeURIComponent(location.href).toLowerCase().includes(a.p))", arg={"l": label.strip(), "p": pid}, timeout=12000)
                        pg.wait_for_selector("[data-item-id]", timeout=12000)
                        pg.wait_for_timeout(DETAIL_SETTLE_MS)
                        info = pg.evaluate(DETAIL_JS)
                        if pid and pid not in unquote(info.get("href", "")).lower():  # 読み終えた詳細欄が、今のカードの施設ではない(前の施設の電話・住所が残っている)
                            got_id = place_id_of(info.get("href", ""))
                            info = None
                            raise RuntimeError(f"詳細欄が別の施設: {got_id!r}")
                        break
                    except Exception as e:
                        last_err = _err_text(e, pg)  # 原文を残す(ブロックと通常の欠損を後から区別するため)
                        pg.wait_for_timeout(2000)
                if info:  # 広告枠の見出しは「スポンサー」の行が先頭に付く。会社名には入れない
                    info = _clean_name(info, label)
                if not info:
                    skipped += 1
                    if len(error_samples) < 3:
                        error_samples.append(f"{label[:30]!r}: {last_err}")
                    continue
                if not usable_name(info["name"]):
                    bad_name += 1  # 名前が空・記号だけ・アイコン文字だけ。行に入れない
                    continue
                key = row_key(info, pid)
                if key in seen:
                    dup += 1
                    continue
                seen.add(key)
                rows.append(_row(info["name"], term, area_name, info["url"], "Googleマップ", info["href"], info.get("tel", ""), info.get("address", "")))
                if pid:
                    rows[-1]["place_id"] = pid  # 下流の重複判定(store_identity_key)が施設の識別子を優先して使う
                time.sleep(PAUSE)
            log(f"  Googleマップ: 候補{n}件のうち 取得{len(rows)}件・読めず{skipped}件・名前が使えず除外{bad_name}件・重複{dup}件")
            if stats is not None:
                stats.update(n=n, skipped=skipped, bad_name=bad_name, cut_short=0 if card_scroll["status"] == "limit_reached" else 1)
            for sample in error_samples:
                log(f"    読めなかった例: {sample}")
        finally:
            if b:
                try:
                    b.close()
                except Exception as e:  # 取り終えたあとの後片付けの失敗(接続が先に切れた等)で、取れた店を捨てない
                    log(f"    （ブラウザを閉じる時の失敗は無視しました: {type(e).__name__}）")
    finally:
        try:
            pw_cm.__exit__(None, None, None)
        except Exception as e:
            log(f"    （ブラウザの後片付けの失敗は無視しました: {type(e).__name__}）")
    return rows


def fetch(source, area_name, term, log, limit=20):
    if source == "google":
        return fetch_google(area_name, term, log, limit)
    if source == "yahoo":
        raise ValueError("Yahoo!マップは取得元から外しました(robots.txt が検索の通信 /proxy を禁止しているため)")
    raise ValueError(f"未対応の取得元です: {source}")
