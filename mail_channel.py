"""メール送信チャネル(第2段・REQ-152)の部品。1通ずつ個別に SMTP で送る。実行環境: Windows / Mac / Linux(標準ライブラリだけ)
ここにあるのは「作る・送る」だけ。送ってよいかの判定(お断り・NG・二重送信・表示義務・時間帯)は autosend 側の共通の関門が先に行う。
認証情報は smtp.json(個人設定・git にも公開物にも入れない)にだけ置く。画面・ログ・メモには値を出さない。
smtp.json: {"host":"","port":465,"security":"ssl|starttls|none","user":"","password":"","from_addr":"(省略可=送信元のメール)","from_name":"(省略可=送信元の氏名)"}"""
import json
import os
import re
import smtplib
import ssl
import unicodedata
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, formataddr
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 認証情報はこのパソコンのローカルの場所に置く(OneDrive など同期されるフォルダの外。Windows の LOCALAPPDATA。無い環境ではツールのフォルダ)
SMTP_FILE = (Path(os.environ["LOCALAPPDATA"]) / "AIまるなげ営業" / "smtp.json") if os.environ.get("LOCALAPPDATA") else (HERE / "smtp.json")
SECURITIES = ("ssl", "starttls", "none")
SUBJECT_LINE = re.compile(r"^\s*件名\s*[:：]\s*(.+?)\s*$")  # 文面の1行目の「件名: …」
ADDR = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
TIMEOUT = 30


class MailError(Exception):
    """送れない(設定・宛先・本文の不備)。理由は利用者に見せられる文面にする(認証情報の値は入れない)"""


def load_settings(path=None):
    """smtp.json を読んで検査する。足りない欄・形の違いは MailError(欄の名前つき)。送信元アドレス・表示名は sender から補う側(build_message)で決める"""
    p = Path(path) if path else SMTP_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise MailError("メール送信の設定(smtp.json)がありません。設定してから送ります")
    except (OSError, ValueError):
        raise MailError("メール送信の設定(smtp.json)を読めません。形を確かめてください")
    if not isinstance(d, dict):
        raise MailError("メール送信の設定(smtp.json)の形が正しくありません")
    for k in ("host", "user", "password"):
        if not str(d.get(k) or "").strip():
            raise MailError(f"メール送信の設定の「{k}」が空です")
    try:
        port = int(d.get("port"))
    except (TypeError, ValueError):
        raise MailError("メール送信の設定の「port」が数字ではありません")
    if not 1 <= port <= 65535:
        raise MailError("メール送信の設定の「port」は1〜65535の数字にしてください")
    sec = str(d.get("security") or "ssl")
    if sec not in SECURITIES:
        raise MailError("メール送信の設定の「security」は ssl / starttls / none のどれかです")
    if sec == "none" and str(d["host"]).strip() not in ("127.0.0.1", "localhost", "::1"):
        raise MailError("メール送信の設定の「security」が none のままでは、パスワードが暗号化されずに流れます。ssl か starttls にしてください")
    if str(d.get("from_addr") or "").strip():
        try:
            clean_address(d["from_addr"])
        except MailError:
            raise MailError("メール送信の設定の「from_addr」のメールアドレスの形が正しくありません")
    out = dict(d, port=port, security=sec)
    return out


def split_subject(body):
    """文面の1行目が「件名: …」なら (件名, 残りの本文)。無ければ ("", 本文のまま)。件名の行は本文に残さない"""
    lines = body.split("\n")
    m = SUBJECT_LINE.match(lines[0]) if lines else None
    if not m:
        return "", body
    return m.group(1), "\n".join(lines[1:]).lstrip("\n")


def clean_address(addr):
    """宛先を NFKC でそろえ、アドレス1件の形か確かめる。複数・改行・表示名つき・空白入りは不可(ヘッダー注入と同報を防ぐ)"""
    a = unicodedata.normalize("NFKC", str(addr or "")).strip()
    if not ADDR.match(a):
        raise MailError("宛先のメールアドレスの形が正しくありません(1件だけ・表示名なし)")
    return a


def _no_break(value, label):
    if re.search(r"[\r\n\x00]", value):
        raise MailError(f"{label}に改行が入っています(送りません)")
    return value


def build_message(settings, sender, to, subject, body):
    """1通分のメール。To は1件だけ(CC・BCC・同報なし)。返信先は送信元のメール。件名・本文が空なら送らない"""
    to = clean_address(to)
    subject = _no_break(str(subject or "").strip(), "件名")
    if not subject:
        raise MailError("件名が空のため送りません")
    if not str(body or "").strip():
        raise MailError("本文が空のため送りません")
    from_addr = clean_address(settings.get("from_addr") or sender.get("メール"))
    from_name = _no_break(str(settings.get("from_name") or sender.get("氏名") or "").strip(), "送信者の表示名")
    reply = clean_address(sender.get("メール") or from_addr)
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to
    msg["Reply-To"] = reply
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.rsplit("@", 1)[-1])
    msg.set_content(body if body.endswith("\n") else body + "\n")
    return msg


def _open(settings):
    """接続して認証まで済ませた SMTP を返す(呼び出し側が閉じる)。失敗は smtplib の例外のまま"""
    host, port, sec = settings["host"], settings["port"], settings["security"]
    if sec == "ssl":
        cli = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
    else:
        cli = smtplib.SMTP(host, port, timeout=TIMEOUT)
    try:
        if sec == "starttls":
            cli.starttls(context=ssl.create_default_context())
        cli.login(settings["user"], settings["password"])
    except BaseException:
        cli.close()
        raise
    return cli


def _setting_failure(e):
    """接続・認証の段階の失敗を「設定:」の理由にする(本文はまだ1文字も送っていない)"""
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "設定: SMTPの認証に失敗しました(ユーザー名・パスワード)"
    if isinstance(e, UnicodeError):  # ユーザー名・パスワードに ASCII 以外が入っている(smtplib が符号化できない)
        return "設定: SMTPのユーザー名・パスワードに使えない文字が入っています"
    return "設定: SMTPサーバーに送れませんでした(" + type(e).__name__ + ")"


def check_login(settings):
    """送信の前の事前確認: 接続と認証だけ試す(送らない)。(True, "") か (False, 「設定:」の理由)。
    送信ログに「送信試行」を書く前に通し、設定の失敗でどの会社も「送った」扱いにならないようにする"""
    try:
        _open(settings).quit()
    except (smtplib.SMTPException, OSError, UnicodeError) as e:
        return False, _setting_failure(e)
    return True, ""


def send(settings, msg):
    """SMTP で1通送る。成功なら (True, メモ)、失敗なら (False, 理由)。認証情報の値は返さない。理由の頭で種類を分ける:
    「設定:」=設定そのものの失敗(未送信が確実・呼び出し側が全体を止める)/「宛先:」=この会社だけの失敗(未送信が確実)/
    「不明:」=本文を送り始めたあとに切れた(届いたか分からない・再送しない)"""
    try:
        cli = _open(settings)
    except (smtplib.SMTPException, OSError, UnicodeError) as e:
        return False, _setting_failure(e)
    try:
        cli.send_message(msg)
    except smtplib.SMTPRecipientsRefused:
        return False, "宛先: 送信先のサーバーがこの宛先を断りました"
    except smtplib.SMTPDataError:
        return False, "宛先: 送信先のサーバーがこの内容を断りました"
    except smtplib.SMTPSenderRefused:
        return False, "設定: SMTPサーバーが送信元のアドレスを断りました"
    except (smtplib.SMTPException, OSError) as e:  # 本文を送り始めたあとの切断・時間切れ。サーバーが受け取った可能性があるので、未送信とは言わない
        return False, "不明: 送信の途中で切れました(" + type(e).__name__ + ")。届いたか受信側で確かめてください。自動では送り直しません"
    finally:
        try:
            cli.quit()  # 送信は済んでいる。終了の挨拶(QUIT)への返事が変でも、結果は変えない
        except (smtplib.SMTPException, OSError):
            cli.close()
    return True, "SMTPで1通送りました"


def sender_identity(settings, sender):
    """実際に使う送信元(From の表示名・アドレスと、つなぐ先)。確認番号と確認画面に入れる(パスワードは入れない)。smtp.json が無ければ None"""
    if not settings:
        return None
    return {"from_addr": settings.get("from_addr") or sender.get("メール"), "from_name": settings.get("from_name") or sender.get("氏名"),
            "host": settings.get("host"), "port": settings.get("port"), "security": settings.get("security"), "user": settings.get("user")}


def is_setting_failure(memo):
    """send の失敗が、その会社ではなく送信設定そのものの失敗か(全体を止める)"""
    return memo.startswith("設定:")
