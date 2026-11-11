"""見張り(2026-11-10・古い版のコピーが画面の番号に居座る問題を、利用者の操作なしで直す)。実行環境: Windows(Mac/Linux は判定だけ動く) / Python 3.9+(標準ライブラリだけ)。
導入先のフォルダ(~/.claude/formlist/app)に置き、タスクスケジューラから「ログオン時(30秒待って)と5分ごと」に1回ずつ動く。1回で終わる小さなプログラム。

1回でやること(画面の番号 8780〜8782 の応答者を whoami で読んで決める):
  ・どれかで作業中(running が true)、または作業中かどうか分からない(running が無い・形が違う) → 何もしない。次の回に回す(R1)
  ・導入先のコピーが 8780 で動いていて最新 → 何もしない(R13: whoami を読むだけ)
  ・別のフォルダのコピーが動いている → 止めて(/api/quit if_idle → 空かなければ、同じユーザーのプロセスだけを止める R24)、導入先を 8780 で起動する(R5)
  ・何も動いていない → 導入先を起動する
  ・導入先の版が古い → 外から更新を当ててから起動する(updater.apply。立ち上がらなければ前の版へ戻す)。更新に失敗しても今の版で起動する(R8)
  ・ほかのアプリが番号を使っている → 止めない。理由を記録する
  ・導入先の必須ファイルが欠けている → 更新の当て直しを、間隔を倍にしながら1日6回まで試す(R6)
  ・同時に1つだけ動く(排他 R3)。入れ替えの後10分はやり直さない。1時間に3回入れ替えたら止まり「入れ替えが繰り返されています」を残す(R3)
  結果: 出力/見張りの記録.txt(何かした・できなかった回だけ1行。200KBで古い分を回す R15)と 出力/見張りの状態.json(毎回。画面の隅が読む R4)。
登録: --install-task で、ログオン時と5分ごとに動くタスクを、今のユーザーの権限で登録する(R14)。名前は1つに固定して上書きし、旧名は消す(R7)。
  --xml-out <パス> は登録せず定義だけ書く(試験用)。

陽性対照: 別のコピーが掴んでいる → 止めて導入先が応答する(test_watchdog.py の実プロセスの場面)。作業中・不明 → 止めない。
誤爆の対照: 導入先が動いていて最新 → 何も止めない。ほかのアプリ → 止めない。導入先を別の書き方で渡しても自分を止めない(R9)。
原理的に取りこぼす型: 応答が2秒を超える忙しいコピー(読めず「ほかのアプリ」と見える→止めない側に倒れる)。whoami も /api/job も持たない、ごく古い版(止めない)。
  タスクスケジューラが使えないPC(会社の制限)。時計の巻き戻しは、未来の時刻の記録を捨てて扱う(単調な時計はプロセスをまたいで比べられないため)。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ports  # noqa: E402

VERSION = "1"  # 見張り自身の版(状態に残す。古い見張りと見分ける)
TASK_NAME = "AIまるなげ営業_画面の常駐"  # 前の版(画面を直接起動していたタスク)と同じ名前で置き換える
OLD_TASK_NAMES = ("AIまるなげ営業_見張り",)  # 旧名の一覧。登録のたびに消す(R7)
INTERVAL = "PT5M"
LOGON_DELAY = "PT30S"  # ログオン直後はネット・OneDrive が未接続のことがあるため待つ(R23)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))
UPDATE_EVERY = 30 * 60  # 導入先が動いていて何も起きていないときに、最新かを公開側へ問い合わせる間隔
COOLDOWN = 10 * 60  # 入れ替えの後、やり直さない時間(R3)
LOOP_WINDOW, LOOP_MAX = 3600, 3  # 1時間に3回入れ替えたら止まる(R3)
LOCK_STALE = 10 * 60  # 排他の印がこれより古ければ、落ちた見張りの残りとみなす
LOG_MAX = 200_000  # 記録の上限(R15)
REQUIRED = ("app.py", "ports.py", "updater.py", "ui.html", "watchdog.py")  # 導入先に無ければ起動しない(R6)
REPAIR_MAX_PER_DAY = 6
WHOAMI_TIMEOUT = 2  # R13
FAIL_FREE = 3  # 続けて失敗してよい回数。超えたら間隔を倍にする(W1)
ROLLBACK_HOLD = 24 * 3600  # 更新して画面が立たず戻した版を、当て直さない時間(F2)。新しい版が公開されれば、その版はすぐ当てる
START_WAIT = 60  # 起動してから応答を待つ上限(秒)
APPLY_FAIL_MAX = 3  # 同じ公開側の版を、続けてこの回数当てられなければ、24時間は当て直さない(R27)
HUMAN_RECENT = 10 * 60  # 画面での操作(保存・開始などの書き込み)がこの秒数以内にあれば、人の操作中とみなして止めない(R1・R12)
SKIP_NOTICE = 12  # 作業中で見送った回数がこれに達したら(5分ごと×12=1時間)、画面とログで「作業が終わったら再起動」を知らせる(R10)。普通の長さの検索・送信のたびに知らせないよう、1時間にした(暫定値・実測で見直す)


def out_dir(here=None):
    d = Path(here or HERE) / "出力"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log(msg, here=None):
    """記録へ1行。上限を超えたら古い分を 見張りの記録.1.txt へ回す(R15)"""
    try:
        p = out_dir(here) / "見張りの記録.txt"
        if p.exists() and p.stat().st_size > LOG_MAX:
            os.replace(p, p.with_name("見張りの記録.1.txt"))
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def read_state(here=None):
    try:
        j = json.loads((out_dir(here) / "見張りの状態.json").read_text(encoding="utf-8"))
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(st, here=None):
    try:
        p = out_dir(here) / "見張りの状態.json"
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def _ages(times, now):
    """記録した時刻からの経過秒。未来の時刻(時計の巻き戻し)は捨てる(R25: 冷却が永久に続かないように)"""
    out = []
    for t in times or []:
        try:
            a = now - float(t)
        except (TypeError, ValueError):
            continue
        if a >= 0:
            out.append(a)
    return out


def norm(p):
    """パスの比較用の形(R9)。大文字小文字・区切り・末尾の区切り・短い名前・リンクをそろえる"""
    if not p:
        return ""
    try:
        q = str(Path(p).resolve())
    except (OSError, RuntimeError, ValueError):
        q = str(p)
    return os.path.normcase(os.path.normpath(q)).rstrip("\\/")


def same(a, b):
    return bool(a) and bool(b) and norm(a) == norm(b)


def pythonw():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        w = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(w):
            return w
    return exe


def _job(port, timeout=None):
    """その番号の応答。誰も待ち受けていない番号は問い合わせない(Windows では空の番号への接続が1回約2秒かかる。破壊検証2回目 N2)"""
    if ports.can_bind(port):
        return None
    return ports.job(port, timeout=WHOAMI_TIMEOUT if timeout is None else timeout)


def busy(j):
    """作業中か。running が明示の false のときだけ「動いていない」。無い・形が違うときは動いているとみなす(R1・R10)"""
    if not (isinstance(j, dict) and j.get("running") is False):
        return True
    # 探す・送るが動いていなくても、送信承認の窓を出している・画面で直近に操作した(保存・開始など)ときは人の操作中(R1・R12。破壊検証2回目 N3)。
    # この2項目を返さない古い版は、従来どおり running だけで判断する
    if j.get("asking"):
        return True
    age = j.get("last_action_age")
    return isinstance(age, (int, float)) and age < HUMAN_RECENT  # 負(時計の巻き戻し)は不明として操作中とみなす(R28)


def quit_idle(port, timeout=3):
    """その番号のツールに「何も動いていなければ終了」を頼む。頼めたら True(終わったかは別に確かめる)"""
    try:
        with _OPEN.open(f"http://127.0.0.1:{port}/", timeout=timeout) as r:
            m = re.search(r"const TOKEN='([^']+)'", r.read(400000).decode("utf-8", "replace"))
        if not m:
            return False
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", data=b'{"if_idle": true}', method="POST",
                                     headers={"X-Token": m.group(1), "Content-Type": "application/json"})
        _OPEN.open(req, timeout=timeout).read()
        return True
    except Exception:
        return False


def listening_pids(port):
    """その番号で待ち受けているプロセス番号(Windows の netstat から)。読めなければ空"""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout
    except Exception:
        return []
    pids = set()
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 5 and p[1].endswith(f":{port}") and p[3].upper() == "LISTENING" and p[4].isdigit():
            pids.add(int(p[4]))
    return sorted(pids)


def pid_user(pid):
    """プロセスの持ち主(DOMAIN\\USER)。読めなければ "" """
    if os.name != "nt":
        return ""
    try:
        out = subprocess.run(["tasklist", "/V", "/FO", "CSV", "/NH", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout
    except Exception:
        return ""
    import csv
    for row in csv.reader(out.splitlines()):
        if len(row) >= 7 and row[1].strip() == str(pid):
            return row[6].strip()
    return ""


def me():
    d, u = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    return f"{d}\\{u}" if d else u


def wait_free(port, sec):
    for _ in range(int(sec * 2)):
        if ports.can_bind(port):
            return True
        time.sleep(0.5)
    return ports.can_bind(port)


def stop_other(port, j, here):
    """別のフォルダのコピーを止める。返す: (止めて番号が空いたか, 理由)。作業が始まったら止めない。止めるのは同じユーザーのプロセスだけ(R24)"""
    quit_idle(port)
    if wait_free(port, 10):
        return True, ""
    again = ports.job(port, timeout=WHOAMI_TIMEOUT)
    if again is not None and busy(again):
        return False, f"{port}番のコピーで作業が始まったため、止めませんでした"
    if again is not None and not same(again.get("here"), j.get("here")):
        return False, f"{port}番の応答者が変わったため、止めませんでした"
    who = me().lower()
    for pid in listening_pids(port):
        owner = pid_user(pid)
        if not owner or owner.lower() != who:
            return False, f"{port}番を持っているプロセス(番号 {pid}・持ち主 {owner or '読めない'})は、このユーザーのものではないため止めていません。パソコンを再起動すると直ります"
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20, creationflags=NO_WINDOW)
    if wait_free(port, 10):
        return True, ""
    return False, f"{port}番で動いている別のフォルダのコピー({j.get('here')})を止められませんでした"


def start_app(here, port):
    """導入先の画面のサーバーを、画面を開かずに起動する。応答したら True"""
    od = out_dir(here)
    env = dict(os.environ, FORMLIST_NO_BROWSER="1", PYTHONIOENCODING="utf-8")
    env.pop("FORMLIST_PORT", None)
    if port != ports.MAIN:
        env["FORMLIST_PORT"] = str(port)
    flags = NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    cmd = [pythonw(), "-X", "utf8", str(Path(here) / "app.py"), "--no-browser"]
    with open(od / "app_起動ログ.txt", "ab") as lg:
        kw = dict(cwd=str(here), env=env, stdin=subprocess.DEVNULL, stdout=lg, stderr=lg, start_new_session=(os.name != "nt"))
        try:  # タスクスケジューラの作業の外で動かす(見張りが終わっても画面のサーバーを道連れにしない)
            proc = subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kw)
        except OSError:
            proc = subprocess.Popen(cmd, creationflags=flags, **kw)
    end = time.time() + START_WAIT  # 待ちの総時間に上限(タスクの制限 PT10M より十分短く。破壊検証2回目 N2)
    while time.time() < end:
        time.sleep(0.5)
        j = _job(port)
        if j is not None and same(j.get("here"), here):
            return True
        if proc.poll() is not None and _job(port) is None:  # 起動したプロセスがすぐ落ちた。待たずに打ち切る
            return False
    return False


def version_wait():
    """更新の起動し直しの後に、新しい版の応答を待つ秒数。updater が新しい版を待つ時間(FORMLIST_RELAUNCH_WAIT・既定180秒)に余裕45秒を足す
    (固定の200秒では、遅いPCで見張りが「失敗」と書いた後に新しい版が立ち上がりうる。破壊検証2回目 N4)"""
    try:
        import updater
        return float(updater.relaunch_wait()) + 45
    except Exception:
        return 225.0


def wait_version(port, here, want, secs):
    """その番号で、このフォルダの画面が版 want で応答するまで待つ。want が空なら、このフォルダが応答すれば良い"""
    end = time.time() + secs
    while time.time() < end:
        j = _job(port)
        if j is not None and same(j.get("here"), here) and (not want or j.get("version") == want):
            return True
        time.sleep(1)
    return False


def need_update(here, apply_failed=None, now=None):
    """更新が要るかだけを確かめる(ファイルは書き換えない)。返す: (状態の文字, 公開側の版, 要るか)。戻した版の保留(24時間)と、
    当てられなかった版の保留(同じ版で APPLY_FAIL_MAX 回続けて当てられなければ24時間。破壊検証3回目 R27)も見る"""
    try:
        import updater
        r = updater.check()
        rv = r.get("remote_version", "")
        if r.get("state") != "古い":
            return r.get("state", "未確認"), rv, False
        if not str(rv or "").strip():  # 版が空・取れない: 保留の鍵が無いので、当てずに見送る(止め直さない。破壊検証4回目 R31)
            return "公開側の版が取れないため、更新は行いません(止め直しもしません)", "", False
        info = updater.skip_info()
        if info and rv and info.get("version") == rv and 0 <= time.time() - float(info.get("last", 0)) < ROLLBACK_HOLD:
            return f"公開されている版は前に更新して画面が立たなかった版のため、{ROLLBACK_HOLD // 3600}時間は当て直しません", rv, False
        af = apply_failed if isinstance(apply_failed, dict) else {}
        if rv and af.get("version") == rv and int(af.get("count", 0) or 0) >= APPLY_FAIL_MAX and 0 <= (time.time() if now is None else now) - float(af.get("last", 0)) < ROLLBACK_HOLD:
            return f"公開されている版({rv})は{af.get('count')}回続けて当てられなかったため、{ROLLBACK_HOLD // 3600}時間は当て直しません", rv, False
        return "古い", rv, True
    except Exception as e:
        return f"更新を確かめられない({type(e).__name__})", "", False


def apply_update(here):
    """導入先が古ければ、外から更新を当てる。返す: (状態の文字, 当てたファイルの一覧, 戻すための控え, 公開側の版)。失敗しても例外にしない(R8)
    前に「更新したが画面が立たず戻した」版は、戻してから ROLLBACK_HOLD 秒は当て直さない(壊れた版が公開されたとき、30分ごとに
    更新→停止→戻す を繰り返さない。2026-11-10 破壊検証 F2)。それより後の再挑戦は updater 側の上限(同じ版は MAX_TRIES 回まで)が効く"""
    try:
        import updater
        r = updater.check()
        if r.get("state") != "古い":
            return r.get("state", "未確認"), [], None, r.get("remote_version", "")
        info = updater.skip_info()
        rv = r.get("remote_version", "")
        if not str(rv or "").strip():  # 版が空・取れない: 当てない(R31)
            return "公開側の版が取れないため、更新は行いません(止め直しもしません)", [], None, ""
        if info and rv and info.get("version") == rv and 0 <= time.time() - float(info.get("last", 0)) < ROLLBACK_HOLD:
            return f"公開されている版は前に更新して画面が立たなかった版のため、{ROLLBACK_HOLD // 3600}時間は当て直しません", [], None, rv
        res = updater.apply()
    except Exception as e:
        return f"更新できない({type(e).__name__})", [], None, ""
    return "更新した", res.get("updated", []), res.get("backup"), res.get("version", "")


def missing_files(here):
    return [n for n in REQUIRED if not (Path(here) / n).is_file()]


def lock(here):
    """同時に1つだけ動く(R3)。取れたら印のパス、取れなければ None"""
    p = out_dir(here) / "見張り.lock"
    for _ in range(2):
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return p
        except FileExistsError:
            try:
                age = time.time() - p.stat().st_mtime
            except OSError:
                continue
            if age < 0 or age > LOCK_STALE:  # 落ちた見張りの残り、または時計の巻き戻し
                try:
                    p.unlink()
                except OSError:
                    return None
                continue
            return None
        except OSError:
            return None
    return None


def run_once(here=None, port=None, check_update=None, now=None, pool=None):
    """1回分。返す: 状態の辞書(action: busy / ok / started / replaced / updated / foreign / failed / locked / cooldown / loop / broken)"""
    here = Path(here or HERE)
    port = port or ports.MAIN
    lk = lock(here)
    if lk is None:
        return {"action": "locked", "reason": "別の見張りが動いているため、この回は何もしませんでした"}
    try:
        return _run(here, port, check_update, now, list(pool or ([port] + [p for p in ports.POOL if p != port] if port == ports.MAIN else [port])))
    finally:
        try:
            lk.unlink()
        except OSError:
            pass


def _run(here, port, check_update, now, pool):
    now = time.time() if now is None else now
    prev = read_state(here)
    st = {"port": port, "here": str(here), "at": now, "at_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "watchdog_version": VERSION,
          "update_checked": prev.get("update_checked", 0), "repair": prev.get("repair", {}), "skipped": int(prev.get("skipped", 0) or 0),
          "apply_failed": prev.get("apply_failed") if isinstance(prev.get("apply_failed"), dict) else {}}
    # この1時間の入れ替えの時刻だけ残す(未来の時刻=時計の巻き戻しは捨てる R25)
    st["replaced_at"] = [t for t in prev.get("replaced_at", []) if _ages([t], now) and _ages([t], now)[0] < LOOP_WINDOW]

    fails = prev.get("fails") if isinstance(prev.get("fails"), dict) else {"count": 0, "last": 0}
    st["fails"] = fails

    def done(action, reason, quiet=False):
        st.update(action=action, reason=reason)
        if action == "failed":  # 続けて失敗した回数(起動・起動し直しの失敗に上限と間隔を付ける。破壊検証 W1)
            st["fails"] = {"count": int(fails.get("count", 0) or 0) + 1, "last": now}
        elif action in ("ok", "started", "replaced", "updated", "restarted"):
            st["fails"] = {"count": 0, "last": 0}
        if not quiet:
            log(reason, here)
        write_state(st, here)
        return st

    lost = missing_files(here)
    if lost:  # 導入先が壊れている(R6)。当て直しは間隔を倍にしながら1日6回まで
        rp = st["repair"] if isinstance(st["repair"], dict) else {}
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if rp.get("day") != day:
            rp = {"day": day, "count": 0, "last": 0}
        wait = 300 * (2 ** max(rp["count"] - 1, 0)) if rp["count"] else 0
        age = _ages([rp.get("last", 0)], now)
        if rp["count"] >= REPAIR_MAX_PER_DAY or (age and age[0] < wait):
            st["repair"] = rp
            return done("broken", f"導入先に必要なファイルがありません({', '.join(lost)})。当て直しは今日{rp['count']}回試しました。次は間隔を空けて試します。直らないときは導入の1行をもう一度実行してください")
        rp.update(count=rp["count"] + 1, last=now)
        st["repair"] = rp
        state, files, _, _ = apply_update(here)
        if missing_files(here):
            return done("broken", f"導入先に必要なファイルがありません({', '.join(missing_files(here))})。更新の当て直し({state})でも直りませんでした(今日{rp['count']}回目)")

    found = {p: _job(p) for p in pool}  # 画面の番号(既定 8780〜8782)。8780 以外のポートを指定したとき(試験)は、その番号だけ
    for p, j in found.items():
        if j is not None and busy(j):  # どこかで作業中、または作業中か分からない(R1)
            st["skipped"] += 1
            why = f"{p}番で「{j.get('name') or '作業'}」が動いている(または動いているか分からない)ため、何もしませんでした(見送り {st['skipped']}回目・次の回に確かめます)。フォルダ: {j.get('here')}"
            if st["skipped"] >= SKIP_NOTICE and not same(j.get("here"), here):  # 強制はしない。画面の隅とログで知らせる(R10)
                why += "。1時間以上入れ替えられていません。作業が終わったら、パソコンを再起動してください"
            return done("busy", why, quiet=same(j.get("here"), here))
    st["skipped"] = 0
    others = {p: j for p, j in found.items() if j is not None and not same(j.get("here"), here)}
    main = found.get(port)
    ours = main is not None and same(main.get("here"), here)
    # 導入先が動いていても、メモリの版がディスクの版と違えば古い版が動いている(更新を当てたが起動し直していない・入れ直したが再起動していない)。
    # whoami の version とディスクの版を比べる。version が無い(whoami の無い古い版)・ディスクの版が読めないときは比べない(不明は止めない R1)(F1)
    want = ports.disk_version(here)
    stale = bool(ours and main.get("version") and want and main.get("version") != want)
    upd_ages = _ages([st["update_checked"]], now)
    upd_due = (not upd_ages or upd_ages[0] >= UPDATE_EVERY) if check_update is None else check_update
    if ours and not others and not upd_due and not stale:
        return done("ok", "導入先のコピーが動いています", quiet=True)
    if main is None and not ports.can_bind(port):
        return done("foreign", f"{port}番を、このツールではない別のアプリ(または{WHOAMI_TIMEOUT}秒以内に応答しないこのツール)が使っています。止めていません。そのアプリを閉じるか、パソコンを再起動すると直ります")
    if others or not ours or stale:  # 入れ替え・起動・起動し直しの前に、繰り返しを止める(R3)
        recent = _ages(st["replaced_at"], now)
        if len(recent) >= LOOP_MAX:
            return done("loop", f"入れ替えが繰り返されています(この1時間に{len(recent)}回)。これ以上は入れ替えません。別のフォルダのツールを起動しているもの(古いショートカット・別の常駐)を消すか、パソコンを再起動してください")
        if others and recent and min(recent) < COOLDOWN:
            return done("cooldown", f"入れ替えから{int(min(recent) // 60)}分しかたっていないため、この回は入れ替えません(10分あける)")
        n = int(fails.get("count", 0) or 0)
        if n >= FAIL_FREE:  # 3回続けて失敗したら、間隔を倍にしながら(最長6時間)試す。起動に失敗する導入先で5分ごとに起動し続けない
            wait = min(300 * 2 ** (n - FAIL_FREE + 1), 6 * 3600)
            age = _ages([fails.get("last", 0)], now)
            if age and age[0] < wait:
                return done("backoff", f"起動・入れ替えが{n}回続けて失敗したため、間隔を空けています(次は{int((wait - age[0]) // 60) + 1}分後)。直らないときは導入の1行をもう一度実行するか、出力/app_起動ログ.txt の末尾を担当者に送ってください")
    replaced = []
    for p, j in others.items():
        ok, why = stop_other(p, j, here)
        if not ok:
            return done("failed", why + "。次の回にもう一度試します")
        replaced.append(j.get("here"))
    if replaced:
        st["replaced_at"].append(now)
    if upd_due:
        st["update_checked"] = now
    alive = ours and _job(port) is not None
    if alive:
        # 動いている導入先: 更新が要るかを先に確かめるだけにし、ファイルは書き換えない。終了を確かめてから当てる
        # (先に当ててから終了を断られると、壊れた版がディスクに残ったまま、前の版へ戻す守りなしで次の回が起動する。破壊検証2回目 N1)
        state, rver, need = need_update(here, prev.get("apply_failed"), now) if upd_due else ("確かめていない", "", False)
        if not need and not stale:
            return done("replaced" if replaced else "ok", (f"別のフォルダのコピー({', '.join(map(str, replaced))})を止めました。" if replaced else "") + f"導入先のコピーが動いています(最新かの確認: {state})", quiet=not replaced)
        st["replaced_at"].append(now)  # 起動し直しも繰り返しの上限(1時間3回)に数える
        what = "新しい版を当てる" if need else f"古い版({main.get('version')})が動いていた(ディスクの版 {want})"
        if not quit_idle(port) or not wait_free(port, 15):
            return done("failed", f"導入先の{what}ため起動し直そうとしましたが、動いている画面が終了に応じませんでした(作業中の可能性)。ファイルは書き換えず、起動し直していません(次の回にもう一度試します)")
        state, files, backup, _rv = apply_update(here) if need else ("確かめていない", [], None, "")
        rver = _rv or rver  # 当てられなかったとき apply_update は版を返さない。確かめたときの公開側の版を使う(保留の鍵)
        apply_miss = need and not files
        if apply_miss:
            what = f"新しい版を当てようとしました({state})が当てられなかった"
            af = prev.get("apply_failed") if isinstance(prev.get("apply_failed"), dict) else {}
            st["apply_failed"] = {"version": rver, "count": (int(af.get("count", 0) or 0) + 1) if af.get("version") == rver else 1, "last": now}
        elif files:
            st["apply_failed"] = {}
        want = ports.disk_version(here)
        if files:
            import updater  # 立ち上がらなければ、起動し直しの小さなプログラムが前の版へ戻し、戻した版を記録する(版を渡す。渡さないと記録されず繰り返す)
            updater.relaunch(port, backup, version=rver)
            vw = version_wait()
            ok = wait_version(port, here, want, vw)
        else:
            vw = 30
            ok = start_app(here, port) and wait_version(port, here, want, 30)
        if ok and apply_miss:  # 画面は戻したが、更新は当たっていない。成功と書かない(R27)
            n = st["apply_failed"]["count"]
            return done("failed", f"更新が当たりません(目標の版 {rver or '不明'}・今の版 {want}・{state})。画面は今の版で起動し直しました。"
                        + (f"同じ版で{n}回続いたため、{ROLLBACK_HOLD // 3600}時間は当て直しません" if n >= APPLY_FAIL_MAX else f"同じ版で{n}回目です。次の確認でもう一度試します"))
        if ok:
            return done("updated" if files else "restarted", f"導入先の{what}ため起動し直し、新しい版({want})が応答しました")
        j = ports.job(port, timeout=WHOAMI_TIMEOUT)
        now_v = (j or {}).get("version") or "応答なし"
        return done("failed", f"導入先の{what}ため起動し直しを試みましたが、{vw if files else 30}秒待っても新しい版({want})が応答しませんでした(今の応答: {now_v})。"
                    + ("更新は前の版へ戻されます。同じ版は24時間当て直しません。" if files else "") + "出力/app_起動ログ.txt の末尾を担当者に送ってください")
    state, files, backup, rver = apply_update(here) if (upd_due or replaced or not ours) else ("確かめていない", [], None, "")
    if start_app(here, port):
        what = f"別のフォルダのコピー({', '.join(map(str, replaced))})を止めて、" if replaced else ""
        return done("replaced" if replaced else "started", f"{what}導入先のコピーを起動しました(最新かの確認: {state}{'・更新したファイル ' + str(len(files)) + '個' if files else ''})")
    return done("failed", f"導入先のコピーを起動しましたが、{port}番で応答しませんでした。出力/app_起動ログ.txt の末尾を担当者に送ってください")


def task_xml(exe, here, user=None, now=None):
    """ログオン時(30秒待つ)と5分ごとに見張りを1回動かすタスクの定義。今のユーザーの権限だけで登録できる形(R14)"""
    from xml.sax.saxutils import escape
    user = user or me()
    script = str(Path(here) / "watchdog.py")
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime((now or time.time()) + 60))  # 登録した直後(ログオンし直す前)からも5分ごとに動かす
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>AIまるなげ営業の見張り(版 {VERSION})。パソコンを開いたときと5分ごとに、導入先の最新の画面が動いているかを確かめ、違えば入れ替えます(作業中は止めません)</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId><Delay>{LOGON_DELAY}</Delay><Repetition><Interval>{INTERVAL}</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></LogonTrigger>
    <TimeTrigger><Enabled>true</Enabled><StartBoundary>{start}</StartBoundary><Repetition><Interval>{INTERVAL}</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(exe)}</Command><Arguments>-X utf8 "{escape(script)}"</Arguments><WorkingDirectory>{escape(str(here))}</WorkingDirectory></Exec></Actions>
</Task>
"""


def _schtasks(args):
    return subprocess.run(["schtasks", *args], capture_output=True, timeout=60, creationflags=NO_WINDOW)


def _text(b):
    for enc in ("utf-16", "utf-8", "mbcs"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def install_task(name=TASK_NAME, here=None, exe=None, xml_out=None, run=None):
    """見張りのタスクを登録する(同じ名前の前の登録は置き換え、旧名は消す R7)。xml_out があれば登録せず定義だけ書く。返す: (成否, 文)"""
    here = Path(here or HERE)
    run = run or _schtasks
    xml = task_xml(exe or pythonw(), here)
    if xml_out:
        Path(xml_out).write_text(xml, encoding="utf-16")
        return True, f"見張りの定義を書きました(登録はしていません): {xml_out}"
    if os.name != "nt":
        return False, "Windows 以外では、見張りの登録をしません"
    tmp = out_dir(here) / "見張りのタスク.xml"
    tmp.write_text(xml, encoding="utf-16")
    r = run(["/Create", "/TN", name, "/XML", str(tmp), "/F"])
    if r.returncode != 0:
        msg = f"見張りの登録(タスクスケジューラ)に失敗しました: {_text(r.stderr or r.stdout).strip()[:300]}。見張りが無くても、画面はデスクトップの「AIまるなげ営業」から開けます"
        log(msg, here)
        return False, msg
    for old in OLD_TASK_NAMES:
        if old != name:
            run(["/Delete", "/TN", old, "/F"])  # 無ければ失敗するだけ
    log(f"見張りを登録しました(ログオン時と5分ごと・版 {VERSION})", here)
    return True, "見張りを登録しました(パソコンを開いたときと5分ごとに動きます)"


def task_has_watchdog(name=TASK_NAME, run=None):
    """その名前のタスクが見張り(watchdog.py)を動かす定義か。無い・読めないときは False"""
    run = run or _schtasks
    try:
        r = run(["/Query", "/TN", name, "/XML"])
    except Exception:
        return False
    return r.returncode == 0 and "watchdog.py" in _text(r.stdout)


def ensure_task(here=None, run=None):
    """画面の起動時に呼ぶ自己修復(R4)。見張りの登録が無い・前の版(画面を直接起動)なら登録し直す。登録し直したら True"""
    if os.name != "nt" or task_has_watchdog(run=run):
        return False
    ok, _ = install_task(here=here, run=run)
    return ok


def status(here=None, now=None):
    """画面の隅に出す見張りの状態。最後に動いてから15分を超えたら警告の文を付ける(R4)"""
    s = read_state(here)
    now = time.time() if now is None else now
    if not s:
        return {"text": "見張り: まだ一度も動いていません", "warn": True}
    age = _ages([s.get("at", 0)], now)
    if not age:  # 最終確認の時刻がこのパソコンの時計より先(時計が戻された)。「0分前」と「15分を超えて」を並べない(W7)
        t, warn = f"見張り: 最終確認の時刻がパソコンの時計より先です・{s.get('action', '')}(時計を合わせると戻ります)", True
    else:
        warn = age[0] > 15 * 60
        t = f"見張り: 最終確認 {int(age[0] // 60)}分前・{s.get('action', '')}"
        if warn:
            t += "(15分を超えて動いていません。パソコンを再起動すると戻ります)"
    if s.get("action") in ("loop", "failed", "broken", "foreign", "backoff") or (s.get("action") == "busy" and int(s.get("skipped", 0) or 0) >= SKIP_NOTICE and "再起動" in str(s.get("reason", ""))):
        warn = True
        t += f"／{s.get('reason', '')}"
    return {"text": t, "warn": warn}


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--install-task" in a:
        name = a[a.index("--task-name") + 1] if "--task-name" in a else TASK_NAME
        xo = a[a.index("--xml-out") + 1] if "--xml-out" in a else None
        exe = a[a.index("--exe") + 1] if "--exe" in a else None
        ok, msg = install_task(name, exe=exe, xml_out=xo)
        print(msg)
        sys.exit(0 if ok else 1)
    try:
        wp = [int(x) for x in (os.environ.get("FORMLIST_WATCH_PORTS") or "").split(",") if x.strip().isdigit()]  # 試験は本物の8780〜8782を避けて別の番号で動かす
        r = run_once(port=wp[0] if wp else ports.MAIN, pool=wp or None)
        print(r.get("action", ""), r.get("reason", ""))
    except Exception as e:
        log(f"見張りが途中で止まりました({type(e).__name__}: {e})")
        sys.exit(1)
