"""AIまるなげ営業を、このパソコンのClaude（デスクトップ版・Claude Code・Codex）に登録する。実行環境: Windows / Python 3.9+。
install.ps1（と、手元のフォルダ用の「MCPで使えるようにする.bat」）から呼ばれる。登録先は見つかったものだけ。どれも見つからないときは、手で登録する設定の文を表示する。
設定ファイルは書き換える前に .bak へ写す。既に formlist が登録されていれば、このフォルダを指すように更新する（二重登録しない）。
引数: --dry-run で書き込まずに内容だけ表示 / --appdata <dir> でデスクトップ版の設定の場所を差し替える（テスト用） / --remove で登録を外す"""
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


def default_appdata() -> str:
    """Windows は APPDATA。Mac のデスクトップ版の設定は ~/Library/Application Support/Claude/ にあるので、その親を返す"""
    env = os.environ.get("APPDATA", "")
    if env or sys.platform != "darwin":
        return env
    return str(Path.home() / "Library" / "Application Support")


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
        return "未導入（Claudeデスクトップ版が見つかりません。入っているのに一度も開いていない場合は、Claude を一度開いて閉じてから、もう一度実行してください）"
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
    try:  # 書いた場所から読み直して確かめる(「登録しました」と出ても Claude から見えない経路を減らす)
        back = json.loads(path.read_text(encoding="utf-8"))
        if back.get("mcpServers", {}).get("formlist") != ENTRY:
            return f"失敗: 書いたはずの設定が読み直しで確かめられませんでした: {path}"
    except (OSError, ValueError) as e:
        return f"失敗: 書いた設定を読み直せませんでした（{e}）: {path}"
    note = "（Python が Microsoft Store 版のため、Claude から見えない場合があります。見えないときは python.org の Python を入れ直してください）" if "WindowsApps" in sys.executable or "PythonSoftwareFoundation" in sys.executable else ""
    return f"登録しました: {path}（Claudeを完全に終了して、開き直すと使えます）{note}"


def remove_desktop(path: Path, dry: bool) -> str:
    if not path.exists():
        return "登録なし"
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        return f"失敗: 設定ファイルが壊れていて読めません（{e}）。触っていません: {path}"
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or "formlist" not in servers:
        return "登録なし"
    if dry:
        return f"(dry-run) {path} から formlist を外します"
    shutil.copy2(path, path.with_suffix(".json.bak"))
    del servers["formlist"]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"外しました: {path}"


def find_exe(name: str):
    """PATH に無いときは、VS Code 系エディタ(Cursor など)の Claude Code 拡張に同梱された claude を探す(2026-10-26 新しいパソコンで PATH に無く登録に失敗した)。
    陽性対照: test_mcp_register_find.py。原理的に取りこぼす型: 上のフォルダ以外に入れた claude / 拡張が古く同梱が無い版"""
    exe = shutil.which(name)
    if exe or name != "claude":
        return exe
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    hits = []
    for base in (".cursor", ".vscode", ".vscode-insiders", ".windsurf", ".vscode-oss"):
        hits += (home / base / "extensions").glob("anthropic.claude-code-*/resources/native-binary/claude*")
    hits += [home / ".claude" / "local" / "claude.exe", home / ".claude" / "local" / "claude", home / ".local" / "bin" / "claude"]  # 最後は Mac/Linux の標準の入れ場所(PATH に無いことがある)
    hits = [h for h in hits if h.is_file() and h.suffix in ("", ".exe")]
    return str(max(hits, key=lambda h: h.stat().st_mtime)) if hits else None


def run_cli(name: str, args: list, dry: bool) -> str:
    exe = find_exe(name)
    if not exe:
        return f"未導入（{name} が見つかりません）"
    if dry:
        return f"(dry-run) {name} " + " ".join(args)
    scope = ["--scope", "user"] if name == "claude" else []
    try:
        # 登録済みなら一度外してから入れ直す（フォルダが変わったときに古い場所を指さないため）
        subprocess.run([exe, "mcp", "remove", "formlist"] + scope, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        r = subprocess.run([exe, "mcp", "add", "formlist"] + scope + ["--"] + args, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"失敗: {name} の応答が60秒ありませんでした。{name} を開いたまま、もう一度実行してください"
    if r.returncode != 0:
        return f"失敗: {name} mcp add が終了コード{r.returncode}。出力: {(r.stdout + r.stderr).strip()[:300]}"
    return f"登録しました（{name}）。{name} を開き直すと使えます"


def remove_cli(name: str, dry: bool) -> str:
    exe = find_exe(name)
    if not exe:
        return f"未導入（{name} が見つかりません）"
    if dry:
        return f"(dry-run) {name} mcp remove formlist"
    try:
        r = subprocess.run([exe, "mcp", "remove", "formlist"] + (["--scope", "user"] if name == "claude" else []), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"失敗: {name} の応答が60秒ありませんでした"
    if r.returncode == 0:
        return "外しました"
    out = (r.stdout + r.stderr).strip()
    return f"登録なし（{name} の表示: {out[:200]}）" if out else "登録なし"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--appdata", default=default_appdata())
    ap.add_argument("--remove", action="store_true", help="登録を外す")
    a = ap.parse_args()
    if a.remove:
        res = {
            "Claudeデスクトップ版": remove_desktop(desktop_config(Path(a.appdata)), a.dry_run) if a.appdata else "未導入（APPDATAが分かりません）",
            "Claude Code": remove_cli("claude", a.dry_run),
            "Codex": remove_cli("codex", a.dry_run),
        }
        for k, v in res.items():
            print(f"{k}: {v}")
        return 1 if any(v.startswith("失敗") for v in res.values()) else 0
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
    if any(v.startswith("登録しました") for v in results.values()):
        for k, v in results.items():
            if v.startswith("失敗"):
                print(f"注意: {k} には登録できませんでした。ほかに登録できたので、このまま進みます")
        return 0
    return 1 if any(v.startswith("失敗") for v in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
