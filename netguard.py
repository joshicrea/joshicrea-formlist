"""通信の境界。外部由来のURL(CSV・ポータル・地図データ・ページ内リンク・転送先)が、この PC や社内のサービスに届かないようにする。
実行環境: Windows / クロスプラットフォーム, Python 3.9+, 標準ライブラリのみ。

check_url(url, strict=False) -> (許可か, 理由)
  - http / https だけ
  - ホスト名が数字の直接指定(127.1・2130706433・0x7f000001・0177.0.0.1・0 など、古い書き方を含む)なら、そのアドレスで判定する
  - 名前なら引いて、全てのアドレスが「公開されているアドレス」であること(私設・ループバック・リンクローカル・予約・共有アドレス帯、
    およびそれを IPv6 に埋め込んだ形(::ffff:127.0.0.1・64:ff9b::7f00:1・6to4・Teredo)は拒否)
  - localhost / .local / .internal などの名前、末尾にドットの付いた形(localhost.)は、引く前に拒否する
  - strict=True は、名前が引けないものも拒否する(ブラウザの通信用。引けない宛先に出す通信は無いので、止めて困らない)
限界(原理的に取りこぼす型): 名前を引いた直後に答えを切り替える攻撃(DNS rebinding)は防げない。確認から接続までの間に別のアドレスへ変わるため。
  プロキシが名前を引く環境では、この PC で引けない名前の扱いがプロキシ次第になる(strict=False のとき)。
  この道具は1人の利用者の PC で動き、相手は営業先の公開サイトなので、通常の誤入力・内部アドレスを含む転送・内部アドレスを指すリンクを止める範囲に限る。
"""
import ipaddress
import socket
import time
from urllib.parse import urlparse

BAD_NAME_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home", ".corp", ".intranet")
_CACHE = {}  # ホスト名 -> (時刻, 結果)
CACHE_SECONDS = 30  # 名前の答えのすり替え(DNS rebinding)が通る時間を短くする


def _embedded_v4(ip):
    """IPv6 に埋め込まれた IPv4 を取り出す(取り出せなければ None)"""
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    packed = ip.packed
    if packed[:12] == b"\x00" * 12 or packed[:12] == bytes.fromhex("0064ff9b0000000000000000"):  # ::a.b.c.d と NAT64(64:ff9b::/96)
        return ipaddress.IPv4Address(packed[12:])
    return None


def is_public_ip(ip):
    if isinstance(ip, ipaddress.IPv6Address):
        v4 = _embedded_v4(ip)
        if v4 is not None:
            return is_public_ip(v4)
    return ip.is_global and not ip.is_multicast


def literal_ip(host):
    """ホスト名が数字の直接指定なら、そのアドレスを返す(古い書き方も含む)。名前なら None"""
    h = (host or "").strip("[]").rstrip(".")
    if not h:
        return None
    try:
        return ipaddress.ip_address(h.split("%")[0])
    except ValueError:
        pass
    try:  # 127.1・2130706433・0x7f000001・0177.0.0.1 など、OS が数字として読む形
        return ipaddress.IPv4Address(socket.inet_ntoa(socket.inet_aton(h)))
    except (OSError, ValueError):
        return None


def host_is_internal_literal(host):
    """名前を引かずに分かる内部宛て"""
    h = (host or "").lower().strip("[]").rstrip(".")
    if not h:
        return True
    if h == "localhost" or h.endswith(BAD_NAME_SUFFIXES):
        return True
    ip = literal_ip(h)
    return ip is not None and not is_public_ip(ip)


def _same_host_as_client(url, host):
    """requests が使う urllib3 が読むホストと、ここで読んだホストが同じか。バックスラッシュなどで読み方が割れると、
    ここでは公開サイト・実際の接続は内部アドレス、になる"""
    try:
        from urllib3.util import parse_url
        h2 = (parse_url(url).host or "").strip("[]").lower().rstrip(".")
    except Exception:  # noqa: BLE001 - 読めないURLは通さない
        return False
    return h2 == (host or "").strip("[]").lower().rstrip(".")


def check_url(url, strict=False):
    if any(c in url for c in "\\") or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return False, "URLにバックスラッシュ・空白・制御文字がある"
    try:
        p = urlparse(url)
        host = p.hostname
    except ValueError:
        return False, "URLの形が不正"
    if "@" in p.netloc:
        return False, "URLにユーザー情報(@)がある"
    if host and not _same_host_as_client(url, host):
        return False, "URLの読み方が割れる(接続先が確定しない)"
    if p.scheme not in ("http", "https"):
        return False, "http/https 以外のURL"
    if not host:
        return False, "ホスト名が無い"
    if host_is_internal_literal(host):
        return False, f"内部や私設のアドレス宛て({host})"
    if literal_ip(host) is not None:
        return True, ""  # 公開アドレスの直接指定
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return (False, f"{host} の名前が引けない") if strict else (True, "")
    for info in infos:
        try:
            if not is_public_ip(ipaddress.ip_address(info[4][0].split("%")[0])):
                return False, f"{host} が内部や私設のアドレスを指している({info[4][0]})"
        except ValueError:
            continue
    return True, ""


def check_url_cached(url, strict=True):
    """ブラウザの通信のように回数が多い所で使う。ホスト名ごとに結果を一定時間覚える"""
    try:
        p = urlparse(url)
    except ValueError:
        return False, "URLの形が不正"
    key = (p.scheme, (p.hostname or "").lower(), p.port, strict)
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    res = check_url(url, strict=strict)
    _CACHE[key] = (now, res)
    return res
