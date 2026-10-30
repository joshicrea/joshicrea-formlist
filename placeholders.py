"""置き換え前の記号と、見本の仮の文字の判定(1か所にまとめたもの)。実行環境: Windows / macOS / Python 3.9+
送信元・件名・文面を「このままでは送らない」と決める判定は、全部この部品を通す(確認の画面・本番の送信・送信元の関門で同じ答えを出すため)。

止めるもの:
  ・置き換え前の記号  {企業名} {書き出し} のような {…}(全角の ｛…｝ も)。20文字以内の中身を記号とみなす
  ・見本の語        【屋号】【拠点エリア】のように、見本(sender.sample.json と 文面ひな形/default.txt)にある【…】。
                   見本の語の一覧は呼ばれるたびに、その2つのファイルから機械で作る(ここに固定の値を書かない)
  ・文の途中の【…】   「ご予約 【予約URL】」のように、行の中に文と並んで置かれた【…】(見本の一覧に無い語でも止める)
  ・1行まるごとの【…】で、見出しの許可リストに無いもの(見本の行の一部を直した行・「【ここにご提案内容を書く】」のような行)
止めないもの:
  ・1行まるごとの【…】で、見出しの許可リストにあるもの(業種ごとの文面の見出し「【企業・団体向け提案書】」)。
    許可リストは heading_words() が、業種別パターン(patterns.PATTERN_DIR)と 文面ひな形/ の各ファイルの「1行まるごとの【…】」から機械で作る
    (見本の語は除く。利用者が書く 文面/ の中のファイルからは作らない=利用者の書いた行が自分を許可しない)

陽性対照(わざと残して止まることを確かめる): 「【屋号】」1行だけ(見本の語)/「ご予約 【予約URL】」(文の途中)/
  「{会社名}さまへ」(記号)/「｛企業名｝」(全角の記号)/見本の文面ひな形そのもの/
  「【この会社にご連絡した理由と、ご提案したいこと】」1行(見本の行から「（2〜3文）」だけ消した形)/「【ここにご提案内容を書く】」1行。
誤爆の対照(止めないことを確かめる): パターンの見出し「【企業向け提案書】」「【企業・団体向け提案書】」の1行/「{企業名}」を置き換えた後の全文/
  資料のリンク「https://example.com/r/?t=ab12」/ known_ok=True のときの {企業名}{書き出し}{署名}{資料URL}。
原理的に取りこぼす入力の型:
  ・括弧を外して書いた見本の語(「拠点エリアで…」)。「氏名」「電話」のような普通の語と区別できないため見ない
  ・許可リストの見出しと一字一句同じ語を、仮の文字のつもりで1行に書いたもの(見出しと区別できない)
  ・〈…〉《…》［…］など、ほかの括弧で書いた仮の文字。21文字以上の {…}
  ・見本のファイルが無い・読めない環境では、見本の語の一覧が空になる(記号と文の途中の【…】と許可リストに無い1行の【…】は引き続き止まる)
誤って止める側の型(安全側): 業種別パターンも見出しの一覧も無い環境(公開版で 文面/パターン/ が無い)で、パターンの見出しを写した利用者の文面は止まる。
  見出しを通すには、その見出しを 文面ひな形/ のファイルか 文面/パターン/ に1行で置く
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLE_SENDER = HERE / "sender.sample.json"
SAMPLE_MESSAGE = HERE / "文面ひな形" / "default.txt"
KNOWN_TOKENS = ("{企業名}", "{書き出し}", "{署名}", "{資料URL}")  # 文面の中で正しく置き換わる記号(文面を保存するときの検査で使う)

BRACE = re.compile(r"[{｛][^}｝]{1,20}[}｝]")  # 以前の3か所の判定と同じ形(緩めない)
BRACKET = re.compile(r"【([^【】\n]{1,200})】")


def sample_words(files=None):
    """見本の語(【】の中身)の集合。sender.sample.json の値と、文面ひな形の本文から抜き出す。読めないファイルは飛ばす"""
    words = set()
    for p in files or (SAMPLE_SENDER, SAMPLE_MESSAGE):
        try:
            text = Path(p).read_text(encoding="utf-8-sig")
        except OSError:
            continue
        if Path(p).suffix == ".json":
            try:
                text = "\n".join(str(v) for v in json.loads(text).values())
            except (ValueError, AttributeError):
                continue
        words |= {m.group(1).strip() for m in BRACKET.finditer(text)}
    return words


def is_heading_line(line):
    """1行がまるごと【…】1つだけか(前後の空白は見ない)"""
    s = (line or "").strip()
    return bool(s) and BRACKET.fullmatch(s) is not None


TEMPLATE_DIR = HERE / "文面ひな形"


def heading_dirs():
    """見出しの許可リストを作るフォルダ: 業種別パターン(patterns.PATTERN_DIR。試験で差し替えられるよう呼ばれるたびに読む)と 文面ひな形/"""
    import patterns
    return (Path(patterns.PATTERN_DIR), TEMPLATE_DIR)


def heading_words(dirs=None, words=None):
    """通してよい1行まるごとの【…】(見出し)の中身の集合。dirs の各フォルダ直下の *.txt から、1行まるごとの【…】を抜き出し、見本の語(words)を除く。
    陽性対照: パターン01〜09の「【企業向け提案書】」・パターン10の「【企業・団体向け提案書】」が入る。
    誤爆の対照(入ってはいけないもの): 文面ひな形/default.txt の「【この会社にご連絡した理由と、ご提案したいこと（2〜3文）】」(見本の語なので除く)。
    原理的に取りこぼす型: フォルダが無い・読めない環境では空になり、見出しも止まる(安全側)。ファイルの中で見出しを見本と区別する印は無いので、
    見本の語の一覧に無い1行の【…】を 文面ひな形/ に置けば、それは見出しとして許可される"""
    words = sample_words() if words is None else words
    out = set()
    for d in dirs if dirs is not None else heading_dirs():
        try:
            files = sorted(Path(d).glob("*.txt"))
        except OSError:
            continue
        for f in files:
            try:
                text = f.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            for line in text.splitlines():
                if is_heading_line(line):
                    out.add(BRACKET.fullmatch(line.strip()).group(1).strip())
    return out - set(words)


def leftovers(text, words=None, known_ok=False, allow_heading=True, headings=None):
    """送れない仮の文字の一覧。空なら送れる。
    返す形: [{"match": 見つけた文字そのもの, "word": 括弧の中身, "kind": "記号"|"見本"|"文の途中"|"見出しでない1行", "line": 行番号(1から)}]
    words: 見本の語の集合(省略時は sample_words())。known_ok=True は文面の保存の検査用で、正しく置き換わる4つの記号を止めない。
    allow_heading=False は送信元の欄(会社名・氏名・件名・署名など)用で、1行まるごとの【…】も止める(欄に見出しは無いため)。
    headings: 通してよい見出しの集合(省略時は heading_words())。1行まるごとの【…】は、この集合にあるときだけ通す"""
    text = text or ""
    words = sample_words() if words is None else words
    if headings is None and allow_heading:
        headings = heading_words(words=words)
    out = []
    for m in BRACE.finditer(text):
        if known_ok and m.group(0) in KNOWN_TOKENS:
            continue
        out.append({"match": m.group(0), "word": m.group(0)[1:-1], "kind": "記号", "line": text.count("\n", 0, m.start()) + 1})
    for i, line in enumerate(text.split("\n"), 1):
        for m in BRACKET.finditer(line):
            word = m.group(1).strip()
            if word in words:
                kind = "見本"
            elif not is_heading_line(line):
                kind = "文の途中"
            elif allow_heading and word in headings:
                continue  # 許可リストの見出し(業種別パターンの【企業・団体向け提案書】など)
            else:
                kind = "見出しでない1行"
            out.append({"match": m.group(0), "word": word, "kind": kind, "line": i})
    for i, line in enumerate(text.split("\n"), 1):  # 閉じていない【 や 】(以前の判定は【 1文字で止めていた。対になった【…】を除いて残るものを見る)
        stray = re.search(r"[【】]", BRACKET.sub("", line))
        if stray:
            out.append({"match": stray.group(0), "word": "", "kind": "文の途中", "line": i})
    return out
