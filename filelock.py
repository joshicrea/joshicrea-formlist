"""複数のプロセス(画面とコマンド、コマンド同士)が同じファイルを書き換えても、互いの追記を消さないための排他とファイルの置き換え。
実行環境: Windows / クロスプラットフォーム, Python 3.9+, 標準ライブラリのみ。

locked(path): path と同じ場所の「<名前>.lock」を OS のロックで確保する。プロセスが落ちればロックは自動で外れる(古いロックが残らない)。
atomic_write(path, text): 同じフォルダの一時ファイルに書いてから置き換える。途中で止まっても、元のファイルが半分だけ書かれた状態にならない。
原理的に取りこぼす型: ロックを使わない他のプログラム(Excel など)が同じファイルを開いている間の書き込みは防げない(その場合は書き込みが失敗し、呼び出し側が止まる)。
"""
import contextlib
import os
import time
from pathlib import Path


@contextlib.contextmanager
def locked(path, timeout=30.0):
    lp = Path(str(path) + ".lock")
    lp.parent.mkdir(parents=True, exist_ok=True)
    f = open(lp, "a+b")
    deadline = time.time() + timeout
    got = False
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.time() > deadline:
                    raise TimeoutError(f"{lp.name} のロックを{timeout:.0f}秒待っても取れませんでした(別の実行が書き込み中です)")
                time.sleep(0.1)
        yield
    finally:
        if got:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def atomic_write(path, text, encoding="utf-8-sig"):
    p = Path(path)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding=encoding)
        for i in range(20):  # ウイルス対策ソフト・同期ソフトが一瞬つかんでいる間は、置き換えが PermissionError になる
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(0.1)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
