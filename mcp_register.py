"""AIまるなげ営業を、このパソコンのClaude（デスクトップ版・Claude Code・Codex）に登録する。実行環境: Windows / Python 3.9+。
「MCPで使えるようにする.bat」から呼ばれる。登録先は見つかったものだけ。どれも見つからないときは、手で登録する設定の文を表示する。
設定ファイルは書き換える前に .bak へ写す。既に formlist が登録されていれば、このフォルダを指すように更新する（二重登録しない）。
引数: --dry-run で書き込まずに内容だけ表示 / --appdata <dir> でデスクトップ版の設定の場所を差し替える（テスト用）"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = str(HERE / "mcp_server.py")
ENTRY = {"command": sys.executable, "args": ["-X", "utf8", SERVER]}


def desktop_config(appdata: Path) -> Path:
    """通常のインストールは APPDATA 直下の Claude フォルダ。Microsoft Store 版は Packages 配下の Claude_* の LocalCache/Roaming/Claude に置かれるので、あればそちらを使う"""
    local = os.environ.get("LOCALAPPDATA", "")
    if local and appdata == Path(os.environ.get("APPDATA", "")):
        for d in sorted((Path(local) / "Packages").glob("Claude_*/LocalCache/Roaming/Claude")):
            return d / "claude_desktop_config.json"
    return appdata / "Claude" / "claude_desktop_config.json"


def register_desktop(path: Path, dry: bool) -> str:
    """Claude デスクトップ版の設定に formlist を足す。フォルダが無い（=未導入）なら何もしない"""
    if not path.parent.exists():
        return "未導入（Claudeデスクトップ版が見つかりません）"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            return f"失敗: 既存の設定ファイルが壊れていて読めません（{e}）。書き換えていません: {path}"
        if not isinstance(data, dict):
            return f"失敗: 既存の設定ファイルの形が想定と違います。書き換えていません: {path}"
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return f"失敗: mcpServers の形が想定と違います。書き換えていません: {path}"
    before = servers.get("formlist")
    servers["formlist"] = ENTRY
    if dry:
        return f"(dry-run) {path} に formlist を{'更新' if before else '追加'}します"
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"登録しました: {path}（Claudeを完全に終了して、開き直すと使えます）"


def run_cli(name: str, args: list, dry: bool) -> str:
    exe = shutil.which(name)
    if not exe:
        return f"未導入（{name} が見つかりません）"
    if dry:
        return f"(dry-run) {name} " + " ".join(args)
    # 登録済みなら一度外してから入れ直す（フォルダが変わったときに古い場所を指さないため）
    subprocess.run([exe, "mcp", "remove", "formlist"] + (["--scope", "user"] if name == "claude" else []),
                   capture_output=True, text=True, timeout=60)
    r = subprocess.run([exe, "mcp", "add", "formlist"] + (["--scope", "user"] if name == "claude" else []) + ["--"] + args,
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return f"失敗: {name} mcp add が終了コード{r.returncode}。出力: {(r.stdout + r.stderr).strip()[:300]}"
    return f"登録しました（{name}）。{name} を開き直すと使えます"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--appdata", default=os.environ.get("APPDATA", ""))
    a = ap.parse_args()
    results = {
        "Claudeデスクトップ版": register_desktop(desktop_config(Path(a.appdata)), a.dry_run) if a.appdata else "未導入（APPDATAが分かりません）",
        "Claude Code": run_cli("claude", [sys.executable, "-X", "utf8", SERVER], a.dry_run),
        "Codex": run_cli("codex", [sys.executable, "-X", "utf8", SERVER], a.dry_run),
    }
    for k, v in results.items():
        print(f"{k}: {v}")
    if not any(v.startswith("登録しました") or v.startswith("(dry-run)") for v in results.values()):
        print("\nどれにも登録できませんでした。Claudeをお使いのアプリの「コネクタ」や「MCP」の設定に、次を入れてください。")
        print(json.dumps({"mcpServers": {"formlist": ENTRY}}, ensure_ascii=False, indent=2))
        return 1
    return 1 if any(v.startswith("失敗") for v in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
