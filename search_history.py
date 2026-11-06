"""探索履歴: 同じエリア×業種で探し直すとき、前回の続きから集める(2026-11-03 ガイアの手紙 formlist-search-resume)。
実行環境: Windows / Linux / WSL2 クロスプラットフォーム(標準ライブラリと filelock のみ)。

組(エリア+業種+キーワード)ごとに、終えた検索の数(jobs_done)と、調べ済みの会社の鍵(checked)を 出力/探索履歴.json に残す。
会社の鍵の作り方は呼び出し側(formlist)が決める。ここは保存と「続きの計画」だけを持つ。
出力する会社の数の数え方(出力数=送れる会社の目標数)は変えない。前回が目標に届かなかったときだけ、前回の分を合わせて数える。"""
import datetime as dt
import json
import re
from pathlib import Path

import filelock

FILE_NAME = "探索履歴.json"
VERSION = 1


def group_key(area, industries, keywords):
    """同じ組かどうかの鍵。順番・空白・全角半角の違いでは別の組にしない"""
    def norm(s):
        return re.sub(r"\s+", "", str(s or "")).casefold()
    terms = sorted({norm(t) for t in list(industries or []) + list(keywords or []) if norm(t)})
    return norm(area) + "|" + ",".join(terms)


def _path(out_dir):
    return Path(out_dir) / FILE_NAME


def _read_all(out_dir):
    try:
        data = json.loads(_path(out_dir).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(out_dir, key):
    """この組の履歴。無い・壊れている・形が違うときは None(最初から探す)"""
    e = (_read_all(out_dir).get("groups") or {}).get(key)
    if not isinstance(e, dict) or not isinstance(e.get("checked"), dict):
        return None
    return e


def save(out_dir, key, entry):
    """この組の履歴を書く。他の組は壊さない。書けなくても探した結果は壊さない(呼び出し側が例外を受ける)"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _path(out_dir)
    with filelock.locked(path):
        data = _read_all(out_dir)
        groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
        cur = groups.get(key)
        if isinstance(cur, dict) and isinstance(cur.get("checked"), dict):
            # 開始時に読んだ履歴から作った entry をそのまま上書きすると、同じ組を同時に走らせた別の検索(別枠)や、途中の保存が書いた調べ済みが消える。
            # いま書かれている分と合わせる(調べ済みは和集合・終えた検索の数と回数は大きい方。それ以外は今回の entry)
            merged = dict(cur["checked"])
            merged.update(entry.get("checked") or {})
            entry = {**entry, "checked": merged,
                     "jobs_done": max(int(cur.get("jobs_done") or 0), int(entry.get("jobs_done") or 0)),
                     "runs": max(int(cur.get("runs") or 0), int(entry.get("runs") or 0))}
        groups[key] = entry
        filelock.atomic_write(path, json.dumps({"version": VERSION, "groups": groups}, ensure_ascii=False, indent=1))


def why_no_history(out_dir, key):
    """この組の履歴が使えない(最初から集める)とき、画面・ログに出す理由の1行。履歴があるときは呼ばない"""
    area_key = key.split("|", 1)[0]
    groups = _read_all(out_dir).get("groups")
    if isinstance(groups, dict) and any(k != key and k.split("|", 1)[0] == area_key for k in groups):
        return "同じ場所の前回の記録はありますが、業種・キーワードの組み合わせが違うため、最初から集めます(組み合わせが前回と同じときだけ、続きから集めます)"
    return "この条件の前回の記録は無いため、最初から集めます"


def forget(out_dir, key):
    """この組の履歴を消す(最初からやり直す)"""
    out_dir = Path(out_dir)
    path = _path(out_dir)
    if not path.exists():
        return
    with filelock.locked(path):
        data = _read_all(out_dir)
        groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
        if groups.pop(key, None) is not None:
            filelock.atomic_write(path, json.dumps({"version": VERSION, "groups": groups}, ensure_ascii=False, indent=1))


def resume_plan(entry, n_all, limit):
    """続きの計画。戻り: dict(start=検索を始める位置, skip=除く会社の鍵の集合, carry=今回の目標から引く前回の送れる会社の数,
    exhausted=前回までで全検索を終えている, note=画面・ログに出す1行)。entry が None なら最初から"""
    if not entry:
        return {"start": 0, "skip": set(), "carry": 0, "exhausted": False, "note": ""}
    done = max(0, min(int(entry.get("jobs_done") or 0), n_all))
    skip = set(entry.get("checked") or {})
    prev_send = int(entry.get("sendable_prev") or 0)
    reached = bool(entry.get("goal_reached"))
    carry = 0 if reached else min(prev_send, max(limit - 1, 0))  # 目標を0以下にしない(今回少なくとも1社は探す)
    note = f"前回の続きから集めます(前回までに {len(skip)}社を調べ済み・検索は全{n_all}件のうち{done}件まで終えています)"
    if carry:
        note += f"。前回の送れる会社 {carry}社と合わせて目標{limit}社に届く形にします(今回の目標は {limit - carry}社)"
    exhausted = bool(n_all) and done >= n_all
    if exhausted:
        note = (f"前回までで、この地域・業種の検索 全{n_all}件をすべて終えています。調べ済みの{len(skip)}社は除きます。"
                "最初からやり直すときは「最初からやり直す」にチェックを入れてください")
    return {"start": done, "skip": skip, "carry": carry, "exhausted": exhausted, "note": note}


def make_entry(prev, jobs_done, n_all, newly_checked, sendable_total, goal_reached):
    """保存する履歴。newly_checked は {会社の鍵: "sendable"|"excluded"}。prev があれば調べ済みを引き継ぐ"""
    checked = dict((prev or {}).get("checked") or {})
    checked.update(newly_checked)
    return {"jobs_done": int(jobs_done), "n_all": int(n_all), "checked": checked, "sendable_prev": int(sendable_total),
            "goal_reached": bool(goal_reached), "runs": int((prev or {}).get("runs") or 0) + 1,
            "updated": dt.datetime.now().isoformat(timespec="seconds")}
