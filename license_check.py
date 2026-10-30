"""利用許可の照合(ライセンス一括管理の第1段・共通契約: AI組織/自走組織/ライセンス台帳API_契約.md の 4)。実行環境: Windows / Mac / Linux(Python 3.9+)
動き: 起動時と24時間ごとに台帳サーバーへ照合する。許可(200)は手元に残し、最後の成功から grace_days(既定7日)は、応答が無い・5xx・通信失敗でも使い続けてよい。
401/403 の確定した拒否は猶予なしで止める。止めるのは「探す・送る」の実行系とMCPの実行で、できたリストの閲覧・書き出しは止めない。
許可キーは、計測キー(計測キー.txt か環境変数 FORMLIST_TRACK_KEY)をそのまま使う(鍵を二重に配らない)。キーの実値は、ログにも応答にも出さない。
台帳サーバーがまだ設置されていない間(404・JSONではない応答)は「未設置」として止めない。設置された時点から、この部品が自動で効き始める。
限界: 利用者のPCで動くため、コードを書き換えられる人には抜かれる。目的は、入れ方を知っていれば誰でも使える状態の解消と、顧客ごと・サービスごとの停止。"""
import json
import math
import os
import random
import re
import secrets
import base64
import hashlib
import threading
import time
from pathlib import Path

import requests

import proposal_links
import state

SERVICE_ID = "formlist"
PRODUCT_VERSION = "1.0.0"  # 製品全体の版(ドット区切りの数字)。台帳の min_version と比べる。製品を更新して公開するたびに上げる(ビルドの印は build として別に送る)
PUBKEY_NAME = "license_sign_pub.pem"  # 配布版に同梱する公開鍵(台帳サーバーの lic_private/sign_pub.pem と同じもの)。無い間は署名を検証しない
CLOCK_SKEW_SECONDS = 86400  # パソコンの時計とサーバー時刻のずれの許容(これを超えて進んだ・戻った時計は、署名つきの成功で直るまで止める)
RETRY_WAITS = (2, 4, 8)  # 初回1回のあとの再試行(最大3回)の待機秒。ゆらぎをつける
URL = "https://ksfactory.joshicrea.com/lic/check.php"
RECHECK_SECONDS = 24 * 3600
RETRY_DENIED_SECONDS = 60  # 拒否や未確認のとき、再度の呼び出しが来ても、この間隔より短くは台帳へ問い合わせない
TIMEOUT = 8
DEFAULT_GRACE_DAYS = 7
MAX_GRACE_DAYS = 14  # サーバーが長い猶予を返しても、締め出しの遅れが無限にならない上限
_LOCK = threading.Lock()


def _url():
    return os.environ.get("FORMLIST_LICENSE_URL") or URL


def cache_path():
    return state.DB.parent / "license_cache.json"


def _load():
    try:
        d = json.loads(cache_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    p = cache_path()
    if not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def install_id():
    """この導入の識別(乱数16桁の16進)。最初に1回だけ作って手元の記録に残す。複数の画面サーバー・MCPで同じ値になる。端末情報は含めない"""
    with _LOCK:
        d = _load()
        iid = d.get("install_id")
        if not (isinstance(iid, str) and re.fullmatch(r"[0-9a-f]{16}", iid)):  # 契約0.4: 形の誤りは400になるので、壊れた値は作り直す
            iid = secrets.token_hex(8)
            d["install_id"] = iid
            _save(d)
        return iid


class _BrokenKey:
    """読めない公開鍵の代わり。どの署名も合わない(検証を外す方向には倒さない)"""
    def verify(self, *a, **k):
        raise ValueError("公開鍵を読めません")


def _pubkey():
    """同梱の公開鍵(試験では FORMLIST_LICENSE_PUBKEY で差し替え)。読めない・形が違うときは None=署名の検証をしない"""
    p = os.environ.get("FORMLIST_LICENSE_PUBKEY") or str(Path(__file__).with_name(PUBKEY_NAME))
    try:
        data = Path(p).read_bytes()
    except OSError:
        return None  # 鍵ファイルが無い(公開鍵を同梱する前の版)
    try:
        from cryptography.hazmat.primitives import serialization
        return serialization.load_pem_public_key(data)
    except Exception:
        return _BrokenKey()  # 鍵ファイルはあるのに読めない・暗号部品が無い: 検証を外さず、止める側に倒す


def _caps_str(caps):
    """署名に入れる機能名の文字列(辞書順・カンマ区切り・無ければ空)。文字列の配列でない・区切り文字を含むときは None"""
    if not isinstance(caps, list) or not all(isinstance(c, str) and c and not any(ch in c for ch in ",|\n") for c in caps):
        return None
    return ",".join(sorted(caps))


def _key_hash():
    """許可キーの sha256 の先頭16桁。キーが無いときは None"""
    k = proposal_links.read_key()
    return hashlib.sha256(k.encode("utf-8")).hexdigest()[:16] if k else None


def signing_string(effective, expires_on, checked_at, grace_days, caps_str, key_hash, install_id_, nonce):
    """契約0.7: v2|service_id|effective|expires_on|checked_at|grace_days|機能名(辞書順・カンマ区切り)|sha256(許可キー)の先頭16桁|X-Install-Id|nonce
    (expires_on が null は空文字)"""
    return "|".join(["v2", SERVICE_ID, str(effective), "" if expires_on is None else str(expires_on), str(checked_at), str(grace_days),
                     caps_str, str(key_hash), str(install_id_), str(nonce)])


def verify_sig(effective, expires_on, checked_at, grace_days, capabilities, nonce, sig, key=None, key_hash=None, install_id_=None):
    """署名(v2)が合えば True。sig が無い・形が違う・鍵が合わないは False。猶予日数・機能・許可キー・導入先も署名に含まれるので、どれを書き換えても合わない"""
    key = key or _pubkey()
    key_hash = key_hash or _key_hash()
    install_id_ = install_id_ or _load().get("install_id")
    caps_str = _caps_str(capabilities)
    if (key is None or caps_str is None or not key_hash or not install_id_ or not isinstance(sig, str)
            or not isinstance(checked_at, str) or not isinstance(nonce, str)
            or not isinstance(grace_days, int) or isinstance(grace_days, bool)):
        return False
    if any("|" in str(v) or "\n" in str(v) for v in (effective, expires_on, checked_at, nonce)):
        return False  # 区切り文字を含む値は、署名対象の分け直しに使えるので受けない
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        key.verify(base64.b64decode(sig, validate=True),
                   signing_string(effective, expires_on, checked_at, grace_days, caps_str, key_hash, install_id_, nonce).encode("utf-8"),
                   padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def _signed_at(c, key):
    """控えの署名が合えば、署名済みの checked_at(サーバー時刻)を epoch 秒で返す。合わない・読めないは None"""
    if not verify_sig("active", c.get("expires_on"), c.get("checked_at"), c.get("grace_days"), c.get("capabilities"), c.get("nonce"), c.get("sig"), key):
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(c["checked_at"].replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _record(update, now):
    """手元の記録を書き換える。キーの実値は入れない"""
    with _LOCK:
        d = _load()
        d.setdefault("first_attempt_at", now)
        d["last_attempt_at"] = now
        d.update(update)
        _save(d)


def _retry_after(r):
    try:
        return max(0, min(60, int((getattr(r, "headers", None) or {}).get("Retry-After", 0))))
    except (TypeError, ValueError):
        return 0


def check(version="", now=None, post=None, retries=0, sleep=time.sleep):
    """台帳サーバーへ照合し、結果を手元に残して返す(status() の形)。version は、ビルドの印(build)。台帳へ送る version は製品の版(PRODUCT_VERSION)。
    retries>0 のとき、通信失敗・5xx・429 のあとに最大 retries 回(待機は RETRY_WAITS にゆらぎ、Retry-After があればそれに従う)やり直す。画面の入口(gate)は待たせないので retries=0。
    ネットワークの失敗は例外にせず、猶予の計算に回す"""
    now = time.time() if now is None else now
    post = post or requests.post
    key = proposal_links.read_key()
    headers = {"Content-Type": "application/json", "X-Install-Id": install_id()}
    if key:
        headers["X-License-Key"] = key
    nonce = secrets.token_urlsafe(24)  # 毎回新しく作る(32文字・英数字と _ -)。応答の署名に含まれ、古い応答の使い回しを防ぐ
    body_out = {"service_id": SERVICE_ID, "version": PRODUCT_VERSION, "build": str(version)[:40], "nonce": nonce}
    r = None
    for attempt in range(retries + 1):
        try:
            r = post(_url(), json=body_out, headers=headers, timeout=TIMEOUT)
        except (requests.RequestException, OSError):
            r = None
        if r is not None and r.status_code != 429 and r.status_code < 500:
            break
        if attempt < retries:
            wait = max(RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)] * random.uniform(0.8, 1.2), _retry_after(r) if r is not None else 0)
            sleep(wait)
    if r is None:
        _record({"last_result": "network"}, now)
        return status(now)
    code = r.status_code
    try:
        body = r.json()
        if not isinstance(body, dict):
            body = {}
    except ValueError:
        body = {}
    if code == 200 and body.get("ok") is True and body.get("effective") == "active":
        key = _pubkey()
        if key is not None and not (body.get("nonce") == nonce and body.get("sig_alg") == "rsa-sha256" and body.get("sig_ver") == "v2"
                                    and verify_sig("active", body.get("expires_on"), body.get("checked_at"), body.get("grace_days"), body.get("capabilities"),
                                                   nonce, body.get("sig"), key, install_id_=headers["X-Install-Id"])):
            _record({"last_result": "bad_signature"}, now)  # 署名が無い・合わない応答は検証失敗。猶予の根拠にしない(最後の成功は更新しない)
            return status(now)
        grace = body.get("grace_days")
        grace = grace if isinstance(grace, int) and 0 <= grace <= MAX_GRACE_DAYS else DEFAULT_GRACE_DAYS
        upd = {"last_result": "ok", "last_ok_at": now, "grace_days": grace, "expires_on": body.get("expires_on"), "denied": None, "not_deployed": False}
        if key is not None:
            upd.update({"sig": body.get("sig"), "nonce": nonce, "checked_at": body.get("checked_at"), "capabilities": body.get("capabilities")})
        _record(upd, now)
    elif code in (401, 403):  # 契約0.7: 署名なしで返る。署名や本文の有無によらず確定した拒否として即時に止め、猶予に回さない
        _record({"last_result": "denied", "denied": {"reason": str(body.get("reason", ""))[:40], "message": str(body.get("message") or "")[:200],
                                                     "expires_on": body.get("expires_on"), "at": now}, "not_deployed": False,
                 "sig": None, "nonce": None, "checked_at": None, "last_ok_at": None}, now)  # 拒否のあと、控えから denied だけを消しても、前回の成功の猶予に戻れないようにする
    elif (code in (404, 410) or (code == 200 and "ok" not in body)) and _pubkey() is None and not (_load().get("last_ok_at") or _load().get("denied")):
        _record({"last_result": "not_deployed", "not_deployed": True}, now)  # 台帳サーバーがまだ無い(この端末が、台帳の正式な応答を一度も受けていない間だけ)。止めない(猶予も使わない)
    else:
        _record({"last_result": f"transient_{code}"}, now)  # 5xx・429・400など。最後の成功からの猶予で続ける
    return status(now)


def status(now=None):
    """手元の記録だけから、いま使ってよいかを決める(通信はしない)。返す dict: allowed / state / message / reason"""
    now = time.time() if now is None else now
    c = _load()
    den = c.get("denied")
    if den:
        den = den if isinstance(den, dict) else {}
        msg = den.get("message") or "利用が許可されていません。担当者にご連絡ください"
        return {"allowed": False, "state": "denied", "reason": den.get("reason", ""), "message": msg, "expires_on": den.get("expires_on")}
    key = _pubkey()
    if key is None:  # 公開鍵を同梱する前の版(従来の動き)
        if c.get("not_deployed"):
            return {"allowed": True, "state": "not_deployed", "reason": "", "message": ""}
        base = c.get("last_ok_at") or c.get("first_attempt_at")
    else:
        # 鍵がある版: 署名つきの成功がある控えだけを猶予の根拠にする。未署名の not_deployed・first_attempt_at・last_ok_at は根拠にしない
        lo = c.get("last_ok_at")
        signed_at = _signed_at(c, key) if isinstance(lo, (int, float)) and not isinstance(lo, bool) and math.isfinite(lo) else None  # NaN・無限大・文字列は根拠にしない(NaNは比較が常に偽で猶予が切れなくなる)
        base = min(c["last_ok_at"], signed_at + CLOCK_SKEW_SECONDS) if signed_at is not None else None  # 手元の時刻を延ばしても、署名済みのサーバー時刻(+時計のずれの許容)より後にはならない
        if base is not None and now < signed_at - CLOCK_SKEW_SECONDS:
            base = None  # 署名済みのサーバー時刻より前=時計が巻き戻っている(控えを書き換えても外せない。署名つきの成功で直る)
    if base is None:
        return {"allowed": False, "state": "unchecked", "reason": "unchecked", "message": "利用の確認がまだできていません。通信を確かめて、もう一度ツールを開いてください"}
    cap = MAX_GRACE_DAYS  # 鍵がある版では grace_days は署名の対象(書き換えると署名が合わず、ここまで来ない)
    grace = c.get("grace_days") if isinstance(c.get("grace_days"), int) and 0 <= c.get("grace_days") <= cap else DEFAULT_GRACE_DAYS
    if now - base > grace * 86400:
        if c.get("last_result") == "ok":  # 直前の照合は成功しているのに猶予切れ=パソコンの時計が進んでいる
            return {"allowed": False, "state": "grace_over", "reason": "grace_over", "message": "パソコンの日付と時刻が正しいか確かめてから、もう一度ツールを開いてください"}
        return {"allowed": False, "state": "grace_over", "reason": "grace_over",
                "message": f"利用の確認が{grace}日以上できていません。通信を確かめて、もう一度ツールを開いてください"}
    ok = c.get("last_result") == "ok"
    return {"allowed": True, "state": "ok" if ok else "grace", "reason": "", "message": "", "expires_on": c.get("expires_on")}


def gate(version="", now=None, post=None):
    """実行系(探す・送る)の入口で呼ぶ。(許可, 利用者向けの文) を返す。未確認のとき、拒否中で前回の問い合わせから一定時間が過ぎたときは、先に照合し直す
    (停止を解いてもらった直後・初回に、画面の再起動を待たせない)"""
    now = time.time() if now is None else now
    s = status(now)
    last = _load().get("last_attempt_at") or 0
    if (s["state"] == "unchecked" or not s["allowed"]) and now - last >= RETRY_DENIED_SECONDS:
        s = check(version, now, post)
    return s["allowed"], s["message"]


def start_background(version="", interval=RECHECK_SECONDS, retries=3):
    """起動時に1回、その後は24時間ごとに照合する(デーモンスレッド)。失敗してもツールは止めない"""
    def loop():
        while True:
            try:
                check(version, retries=retries)
            except Exception:
                pass
            time.sleep(interval)
    t = threading.Thread(target=loop, name="license-check", daemon=True)
    t.start()
    return t
