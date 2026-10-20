"""営業資料の文章から、営業文のたたき台を作る(AIは使わない。資料の見出し・箇条書き・URLを拾って、型に入れるだけ)。"""
import re

BULLET = re.compile(r"^\s*(?:[・\-\*•●■□▶▪◆◇]|\d+[\.\)）、]|[①-⑩])\s*(.+)$")
URL = re.compile(r"https?://[^\s<>\"'）)」』]+")
MAX_POINTS = 4


def _clean(line):
    return re.sub(r"^#+\s*", "", line).strip().strip("【】[]")


def make_sample(material, sender=None):
    """戻り値: {"text": 営業文, "title": 拾った題, "points": 拾った箇条書き, "url": 拾ったURL}。拾えなかった項目は空"""
    sender = sender or {}
    lines = [l.rstrip() for l in (material or "").splitlines()]
    body = [l for l in lines if l.strip()]
    if not body:
        raise ValueError("資料の文章が空です")
    title = ""
    for l in body:
        c = _clean(l)
        if c and not BULLET.match(l) and not URL.fullmatch(c) and len(c) <= 40:
            title = c
            break
    points = []
    for l in body:
        m = BULLET.match(l)
        if m and 4 <= len(m.group(1).strip()) <= 80:
            points.append(m.group(1).strip())
        if len(points) >= MAX_POINTS:
            break
    if not points:  # 箇条書きが無い資料は、短めの文を拾う
        for s in re.split(r"[。\n]", re.sub(r"\s+", " ", material)):
            s = s.strip()
            if 10 <= len(s) <= 60 and s != title and not URL.search(s):
                points.append(s)
            if len(points) >= 3:
                break
    urls = URL.findall(material)
    url = urls[0] if urls else ""
    who = " ".join(x for x in (sender.get("会社名", ""), sender.get("氏名", "")) if x and "【" not in x)
    out = ["お問い合わせフォームより失礼いたします。", "", "{書き出し}", ""]
    out.append((who + "と申します。" if who else "") + (f"{title}について、" if title else "") + "ご提案したくご連絡いたしました。")
    if points:
        out += ["", "内容は、次のとおりです。", ""] + ["・" + p for p in points]
    if url:
        out += ["", "くわしい資料は、こちらからご覧いただけます。", url]
    out += ["", "もしご興味をお持ちいただけましたら、", "お時間をいただき、", "貴社のご状況やご希望をお聞かせいただけないでしょうか。", "", "どうぞよろしくお願いいたします。", "", "{署名}"]
    return {"text": "\n".join(out) + "\n", "title": title, "points": points, "url": url}
