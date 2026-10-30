#!/bin/bash
# AIまるなげ営業 インストールスクリプト（実行環境: macOS / bash 3.2 以上。Windows は install.ps1）
# 対象ユーザー: Mac を使う営業ツールの利用者（IT初心者を含む）。操作は Claude Code に「次の1行をターミナルで実行して」と頼むだけで完結する（利用者が自分でターミナルを開く必要はない）
# 利用者向けの手順は 導入と使い方.md。実行する1行:
#   f=$(mktemp "${TMPDIR:-/tmp}/formlist_install.XXXXXX") && curl -fsSL https://raw.githubusercontent.com/joshicrea/joshicrea-formlist/master/install.sh -o "$f" && bash "$f"
#
# やること: Python 3.10 以上を確認（無ければ Homebrew で入れる）→ ツール本体を ~/.claude/formlist/app に置く
#           → 専用の仮想環境に部品と専用ブラウザを入れて、読み込めるか確かめる
#           → Claude（デスクトップ版・Claude Code・Codex のうち見つかったもの）に登録する → ログインのたびに画面を用意する常駐（launchd）を登録する
# もう一度実行すると更新になる。送信元・文面・送らないリスト・できたリストは消えない。
# 指定:
#   --uninstall  撤去。動いている画面を止め、登録を外し、本体を消す。設定と出力は残す
#   --open       画面を開く（ブラウザで起動.command から呼ぶ）。動いていればそれを開き、無ければ起動する
#   検証用: --local-source <フォルダ> 手元から入れる / --target-root <フォルダ> 置き場所を変える / --skip-deps 仮想環境・部品・専用ブラウザの導入と確認を飛ばす
#           --dry-run-register 登録と登録の取り外しを試走にする（Claude の設定を書き換えない） / --no-resident 常駐を登録しない
#   環境変数 FORMLIST_LAUNCHD_DIR で常駐の設定ファイルの置き場を変える（試走でも、これを指定すると作る）。FORMLIST_ALLOW_NON_MAC=1 で Mac 以外でも動かす（検証用）
# 守備範囲: Claude の画面から実際にツールが見えるかは、このスクリプトでは確かめない（完了の表示にもそう出す）。
# 書き方の注意: Mac 標準の bash は 3.2。連想配列・mapfile・${var,,} は使わない。sed -i・readlink -f・realpath も使わない（BSD 版で引数が違う）。
#   Python の呼び出しは標準入力を /dev/null にする（curl で流し込んだ場合に、スクリプトの続きを読み取られないため）。
#   Homebrew の Python は pip の直接導入を断る（externally-managed-environment）ので、部品は専用の仮想環境 ~/.claude/formlist/venv に入れる。
#   本人専用の設定 zip の取り込みは持たない（設定の文を Claude Code に貼る方式。install.ps1 の -ProfileZip に相当するものは作らない）。

REPO="joshicrea/joshicrea-formlist"
ROOT="${FORMLIST_ROOT:-$HOME/.claude/formlist}"
LOCAL_SOURCE=""; SKIP_DEPS=0; DRY_REG=0; UNINSTALL=0; OPEN=0; NO_RESIDENT=0
LABEL="jp.joshicrea.formlist"
PLIST_DIR="${FORMLIST_LAUNCHD_DIR:-$HOME/Library/LaunchAgents}"
MANIFEST=".install_files.txt"
KEEP="sender.json message_adoptions.json 出力 文面 計測キー.txt"
ASK="表示をそのままコピーして、このツールをお渡しした担当者に送ってください。"
HERE="$(cd "$(dirname "$0")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --local-source) LOCAL_SOURCE="${2:-}"; shift 2 ;;
    --target-root) ROOT="${2:-}"; shift 2 ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --dry-run-register) DRY_REG=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --open) OPEN=1; shift ;;
    --no-resident) NO_RESIDENT=1; shift ;;
    *) echo "知らない指定です: $1"; exit 2 ;;
  esac
done
APP="$ROOT/app"
VENV="$ROOT/venv"
PLIST="$PLIST_DIR/$LABEL.plist"

if [ "$(uname -s)" != "Darwin" ] && [ -z "${FORMLIST_ALLOW_NON_MAC:-}" ]; then
  echo "このスクリプトは Mac 用です。Windows は install.ps1 を使ってください。"; exit 1
fi

die() { echo "$1"; exit "${2:-1}"; }

# Python 3.10 以上を探す。/usr/bin/python3 は呼ばない（開発ツール未導入の Mac では、呼ぶだけで導入ダイアログが出るスタブで、入っていても 3.9 のため使えない）
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' </dev/null >/dev/null 2>&1; }
find_python() {
  local c p
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    [ "$p" = "/usr/bin/python3" ] && continue
    py_ok "$p" && { echo "$p"; return 0; }
  done
  for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 /Library/Frameworks/Python.framework/Versions/3.10/bin/python3; do
    [ -x "$p" ] || continue
    py_ok "$p" && { echo "$p"; return 0; }
  done
  return 1
}

# curl の失敗を、原因が分かる言葉にする（$1=終了コード $2=何をしていたか）
net_error() {
  echo "$2 できませんでした。"
  case "$1" in
    6) echo "原因: インターネットにつながっていないか、github.com の名前が引けません。ネットワークを確かめてください。" ;;
    35|51|58|60) echo "原因: 会社のネットワークの証明書の検査に止められています。ネットワーク管理者に、github.com と pypi.org を検査の対象外にしてもらってください。" ;;
    22) echo "原因: 接続先がエラーを返しました（会社のプロキシの認証 407 や、ページが無い 404 など）。ネットワーク管理者に、github.com と pypi.org への接続を許可してもらってください。" ;;
    *) echo "原因: ネットワークの問題と思われます。つながっているか確かめて、もう一度実行してください。" ;;
  esac
  echo "（curl の終了コード: $1）"
}

# 動いているこのフォルダの画面を止める。$2=1 なら、何かの処理中でも止めない。本番の送信中は常に止めない。止めなかったときの終了コードは3
STOP_PY=""
IFS= read -r -d '' STOP_PY <<'PYEOF' || true
import json, os, re, subprocess, sys, time, urllib.request
here, strict = os.path.realpath(sys.argv[1]), sys.argv[2] == "1"
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def whoami(port):
    try:
        with op.open("http://127.0.0.1:%d/api/whoami" % port, timeout=3) as r:
            j = json.loads(r.read().decode("utf-8"))
        return j if isinstance(j, dict) and j.get("tool") == "formlist" else None
    except Exception:
        return None
mine = []
for port in (8780, 8781, 8782):
    j = whoami(port)
    if j and j.get("here") and os.path.realpath(j["here"]) == here:
        if j.get("running") and (strict or j.get("name") == "本番送信"):
            print("画面（%d番）で「%s」が動いています。終わってから、もう一度実行してください。" % (port, j.get("name")))
            sys.exit(3)
        mine.append(port)
for port in mine:
    try:
        with op.open("http://127.0.0.1:%d/" % port, timeout=3) as r:
            m = re.search(r"const TOKEN='([^']+)'", r.read().decode("utf-8"))
        if m:
            op.open(urllib.request.Request("http://127.0.0.1:%d/api/quit" % port, data=b"{}", method="POST",
                    headers={"X-Token": m.group(1), "Content-Type": "application/json"}), timeout=3).read()
    except Exception:
        pass
    for _ in range(20):
        time.sleep(0.5)
        if whoami(port) is None:
            break
    else:  # 終了の依頼に応じなかった。待ち受けているプロセスを止める
        pids = subprocess.run(["lsof", "-ti", "tcp:%d" % port, "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
        for pid in pids:
            try:
                os.kill(int(pid), 15)
            except OSError:
                pass
    print("動いていた画面（%d番）を止めました" % port)
PYEOF

# 残っているプロセス（Claude が起動した mcp_server.py・送る処理など、コマンドにこのフォルダを含むもの）を止める。自分と親（このスクリプト）は除く
KILL_PY=""
IFS= read -r -d '' KILL_PY <<'PYEOF' || true
import os, subprocess, sys
app, me = sys.argv[1], int(sys.argv[2])
for line in subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout.splitlines():
    pid_s, _, cmd = line.strip().partition(" ")
    if not pid_s.isdigit():
        continue
    pid = int(pid_s)
    if pid in (os.getpid(), os.getppid(), me) or app not in cmd or "install.sh" in cmd or "python" not in cmd.lower():
        continue
    try:
        os.kill(pid, 15)
    except OSError:
        pass
PYEOF

stop_servers() {  # $1=python $2=厳密(1) か
  [ -f "$APP/app.py" ] || return 0
  "$1" -X utf8 -c "$STOP_PY" "$APP" "${2:-0}" </dev/null
}

xml_esc() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

launchd_unload() {
  command -v launchctl >/dev/null 2>&1 || return 0
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1 || true
}

# ログインのたびに画面のサーバーを起動し、5分ごとに「止まっていれば起動し直す」。1つだけ動く（app.py が、すでに動いていれば何もしない）。
# KeepAlive は使わない: 動いているときに app.py がすぐ終わるので、起動し直しが空回りし続け、「ツールを終了する」も効かなくなる
set_resident() {  # $1=python
  mkdir -p "$PLIST_DIR" "$APP/出力" || return 1
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_esc "$1")</string><string>-X</string><string>utf8</string><string>$(xml_esc "$APP")/app.py</string><string>--no-browser</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_esc "$APP")</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONIOENCODING</key><string>utf-8</string></dict>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$(xml_esc "$APP")/出力/画面_常駐ログ.txt</string>
  <key>StandardErrorPath</key><string>$(xml_esc "$APP")/出力/画面_常駐ログ.txt</string>
</dict>
</plist>
EOF
  [ -s "$PLIST" ] || return 1
  command -v launchctl >/dev/null 2>&1 || { echo "（launchctl が無いため、常駐の設定ファイルを置くだけにしました: $PLIST）"; return 0; }
  launchd_unload
  launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || launchctl load -w "$PLIST" >/dev/null 2>&1
}

# --- 画面を開く (--open)。終了コード 0=開いた / 3=原因を表示して止まった ---
if [ "$OPEN" = 1 ]; then
  PY="$(find_python)" || die "Python が見つかりません。導入と使い方の「1 導入」を、もう一度行ってください。" 3
  [ -x "$VENV/bin/python" ] && py_ok "$VENV/bin/python" && PY="$VENV/bin/python"
  mkdir -p "$HERE/出力"
  LOGF="$HERE/出力/画面_起動ログ.txt"
  ( cd "$HERE" && nohup "$PY" -X utf8 app.py </dev/null >>"$LOGF" 2>&1 & echo $! > "$HERE/出力/.open_pid" )
  OPID="$(cat "$HERE/出力/.open_pid" 2>/dev/null)"; rm -f "$HERE/出力/.open_pid"
  i=0
  while [ "$i" -lt 30 ]; do
    sleep 0.5; i=$((i + 1))
    for p in 8780 8781 8782; do
      if curl -fs --noproxy '*' -m 2 "http://127.0.0.1:$p/api/whoami" 2>/dev/null | grep -q formlist; then
        echo "画面を開きます: http://127.0.0.1:$p/（自動で開かないときは、このアドレスをブラウザに入れます）"; exit 0
      fi
    done
    if [ -n "$OPID" ] && ! kill -0 "$OPID" 2>/dev/null; then break; fi  # 起動した app.py が先に終わった（8780番を別のアプリが使っている等）
  done
  echo "画面を起動できませんでした。$ASK"
  echo "（元のメッセージ）"; tail -n 15 "$LOGF"
  exit 3
fi

echo ""
if [ "$UNINSTALL" = 1 ]; then echo "AIまるなげ営業を撤去しています..."; else echo "AIまるなげ営業をインストールしています..."; fi
echo ""

# --- 撤去 (--uninstall) ---
# 動いている画面を止め、登録を外し、本体を消す。送信の記録・設定・できたリスト(出力)は、消さずに残す
if [ "$UNINSTALL" = 1 ]; then
  bad=0
  PY="$(find_python)" || PY=""
  [ -x "$VENV/bin/python" ] && py_ok "$VENV/bin/python" && PY="$VENV/bin/python"
  if [ -n "$PY" ]; then
    stop_servers "$PY" 1 || exit 1
  fi
  launchd_unload
  rm -f "$PLIST"
  if [ -z "$PY" ]; then
    echo "Python が見つからないので、Claude の登録を自動では外せません。Claude の設定の「開発者」(Developer) から formlist を削除してください。"; bad=1
  elif [ -f "$APP/mcp_register.py" ]; then
    rest=""; [ "$DRY_REG" = 1 ] && rest="--dry-run"
    ( cd "$APP" && "$PY" -X utf8 mcp_register.py --remove $rest </dev/null ) || bad=1
    "$PY" -X utf8 -c "$KILL_PY" "$APP" "$$" </dev/null
  else
    echo "本体が見つかりません: $APP（すでに撤去済みかもしれません）"
  fi
  if [ -d "$APP" ]; then
    keep="$ROOT/撤去前の設定と出力_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$keep"
    for n in $KEEP; do [ -e "$APP/$n" ] && cp -R "$APP/$n" "$keep/"; done
    sleep 1
    rm -rf "$APP" "$VENV"
    if [ -d "$APP" ]; then echo "本体のフォルダの一部を消せませんでした（使用中の可能性）。パソコンを再起動してから、このフォルダを手で消してください: $APP"; bad=1
    else echo "本体を消しました。設定と出力は、ここに残してあります: $keep"; fi
  fi
  echo "送信の記録（送らない会社・送信済みの会社）は、残してあります: $HOME/Library/Application Support/FormListTool"
  [ "$DRY_REG" = 1 ] && echo "（試走のため、Claude の登録は外していません）"
  if [ "$bad" != 0 ]; then echo "撤去の途中で、うまくいかなかったところがあります。上の表示を見てください。$ASK"; exit 1; fi
  echo "撤去が終わりました。Claude を完全に終了して開き直すと、AIまるなげ営業が消えています。"
  exit 0
fi

# --- 1. Python ---
PY="$(find_python)"
if [ -z "$PY" ]; then
  echo "Python 3.10 以上が見つからないので、入れます（数分かかります）..."
  BREW="$(command -v brew 2>/dev/null)"
  [ -z "$BREW" ] && [ -x /opt/homebrew/bin/brew ] && BREW=/opt/homebrew/bin/brew
  [ -z "$BREW" ] && [ -x /usr/local/bin/brew ] && BREW=/usr/local/bin/brew
  if [ -z "$BREW" ]; then
    echo "Homebrew が無いため、自動では入れられません。https://www.python.org/downloads/macos/ の「macOS 64-bit universal2 installer」を入れてから、同じ手順をもう一度実行してください。"
    exit 1
  fi
  "$BREW" install python@3.12 </dev/null
  PY="$(find_python)"
  if [ -z "$PY" ]; then
    echo "Python を入れましたが、まだ見つかりません。このターミナル（Claude Code から実行したときは Claude Code）を一度閉じて開き直し、同じ手順をもう一度実行してください。"
    exit 1
  fi
fi
echo "Python: $PY"

# --- 2. ツール本体を取得 ---
WORK="$(mktemp -d "${TMPDIR:-/tmp}/formlist-install.XXXXXXXX")" || die "作業用のフォルダを作れませんでした。"
trap 'rm -rf "$WORK"' EXIT
if [ -n "$LOCAL_SOURCE" ]; then
  [ -f "$LOCAL_SOURCE/mcp_server.py" ] || die "--local-source に mcp_server.py がありません: $LOCAL_SOURCE"
  SRC="$LOCAL_SOURCE"
  echo "手元のフォルダから入れます: $SRC"
else
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/master.tar.gz" -o "$WORK/src.tgz" </dev/null
  rc=$?
  [ "$rc" != 0 ] && { net_error "$rc" "GitHub からツール本体を取得"; exit 1; }
  tar -xzf "$WORK/src.tgz" -C "$WORK" || die "取得したファイルを開けませんでした。何も置いていません。"
  SRC="$(ls -d "$WORK"/joshicrea-formlist-* 2>/dev/null | head -n 1)"
  { [ -n "$SRC" ] && [ -f "$SRC/mcp_server.py" ]; } || die "取得したものの中に mcp_server.py がありません。何も置いていません。"
fi

# --- 3. 配置（本人の設定と結果は残す。前の版にあって今の版に無いファイルは消す） ---
stop_servers "$PY" 1 || exit 1   # 古い版の画面が動いたままだと、入れ替えた中身が使われない
launchd_unload                    # 入れ替えの途中で、常駐が古い版を起動し直さないように
mkdir -p "$APP" || die "置き場所を作れませんでした: $APP"
ls -1 "$SRC" | grep -v -e '^__pycache__$' > "$WORK/names.txt"
if [ -f "$APP/$MANIFEST" ]; then
  while IFS= read -r n; do
    [ -z "$n" ] && continue
    case "$n" in .*|*/*) continue ;; esac
    case " $KEEP " in *" $n "*) continue ;; esac
    grep -Fxq -- "$n" "$WORK/names.txt" || rm -rf "$APP/$n"
  done < "$APP/$MANIFEST"
fi
while IFS= read -r n; do
  [ -z "$n" ] && continue
  case " $KEEP " in *" $n "*) [ -e "$APP/$n" ] && continue ;; esac   # すでにあれば上書きしない
  if [ -d "$SRC/$n" ]; then rm -rf "$APP/$n"; cp -R "$SRC/$n" "$APP/$n"; else cp "$SRC/$n" "$APP/$n"; fi
done < "$WORK/names.txt"
cp "$WORK/names.txt" "$APP/$MANIFEST"
chmod +x "$APP"/*.command "$APP"/install.sh 2>/dev/null
[ -f "$APP/sender.json" ] || { [ -f "$APP/sender.sample.json" ] && cp "$APP/sender.sample.json" "$APP/sender.json"; }   # 最初は、送信元のひな形から始める
mkdir -p "$APP/出力"
PROFILE_NOTE=""
if [ ! -f "$APP/sender.json" ] || { [ -f "$APP/sender.sample.json" ] && cmp -s "$APP/sender.json" "$APP/sender.sample.json"; }; then
  PROFILE_NOTE="送信元はまだ見本のままです。Claude Code に、担当者から届いた「設定の文」を貼って送ると、送信元と文面が入ります（導入と使い方の「2 最初の1回だけ」）。"
fi
echo "配置しました: $APP"

# --- 4. 専用の仮想環境・部品・専用ブラウザ。入れたあとに、読み込めるか確かめる ---
RPY="$PY"
CHECKED=""
if [ "$SKIP_DEPS" != 1 ]; then
  if [ -x "$VENV/bin/python" ] && ! "$VENV/bin/python" -c pass </dev/null >/dev/null 2>&1; then rm -rf "$VENV"; fi   # 土台の Python が入れ替わって壊れた仮想環境は作り直す
  if [ ! -x "$VENV/bin/python" ]; then
    "$PY" -m venv "$VENV" </dev/null || die "専用の仮想環境を作れませんでした。$ASK"
  fi
  RPY="$VENV/bin/python"
  echo "部品を入れています（数分かかります）..."
  "$RPY" -m pip install --quiet --disable-pip-version-check "playwright==1.58.0" "requests>=2.32,<3" "beautifulsoup4>=4.12,<5" "openpyxl>=3.1,<4" "mcp>=1.29,<2" "pydantic>=2.10,<3" </dev/null
  rc=$?
  [ "$rc" != 0 ] && die "部品の導入に失敗しました（終了コード $rc）。上の表示に「CERTIFICATE_VERIFY_FAILED」があれば、会社のネットワークの証明書検査が原因です。「407」があればプロキシの認証が原因です。それ以外は、$ASK"
  echo "ホームページを開くための専用ブラウザを入れています（数分かかります）..."
  "$RPY" -m playwright install chromium </dev/null
  rc=$?
  [ "$rc" != 0 ] && die "専用ブラウザの導入に失敗しました（終了コード $rc）。上の表示に「self signed certificate」があれば、会社のネットワークの証明書検査が原因です。それ以外は、$ASK"
  CHECK_PY="import importlib.util, os, sys; miss=[m for m in ('playwright','requests','bs4','openpyxl','mcp','pydantic','certifi') if importlib.util.find_spec(m) is None]; print('MISSING ' + ' '.join(miss)) if miss else None; sys.exit(2) if miss else None; from mcp.server.fastmcp import FastMCP; from playwright.sync_api import sync_playwright; pw=sync_playwright().start(); exe=pw.chromium.executable_path; pw.stop(); ok=os.path.exists(exe); print(('CHROMIUM ' if ok else 'NOCHROMIUM ') + exe); sys.exit(0 if ok else 3)"
  "$RPY" -X utf8 -c "$CHECK_PY" </dev/null
  rc=$?
  [ "$rc" != 0 ] && die "部品か専用ブラウザが、入れたあとも読み込めませんでした（終了コード $rc）。同じ手順をもう一度実行してください。それでも同じなら、$ASK"
  CHECKED="部品が読み込めること。専用ブラウザがあること。"
fi

# --- 5. Claude への登録 ---
echo "Claude に登録しています..."
rest=""; [ "$DRY_REG" = 1 ] && rest="--dry-run"
( cd "$APP" && "$RPY" -X utf8 mcp_register.py $rest </dev/null )
rc=$?
[ "$rc" != 0 ] && die "Claude への登録で問題が起きました（終了コード $rc）。$ASK"

# --- 6. 常駐（ログインのたびに画面を用意する） ---
RESIDENT_OK=0
if [ "$NO_RESIDENT" != 1 ] && { [ "$DRY_REG" != 1 ] || [ -n "${FORMLIST_LAUNCHD_DIR:-}" ]; }; then
  if set_resident "$RPY"; then RESIDENT_OK=1
  else echo "常駐の登録（launchd）を作れませんでした。画面は、$APP の「ブラウザで起動.command」を押すと開きます。"; fi
fi

echo ""
echo "インストールが終わりました。"
msg="確かめたこと: ${CHECKED}Claude への登録を書いたこと"
[ "$DRY_REG" = 1 ] && msg="${msg}（試走のため、実際には書いていません）"
msg="${msg}。"
[ "$RESIDENT_OK" = 1 ] && [ "$DRY_REG" != 1 ] && msg="${msg}常駐の設定を登録したこと。"
echo "$msg"
if [ "$RESIDENT_OK" = 1 ] && [ "$DRY_REG" != 1 ]; then
  echo "画面のアドレスは、いつも http://127.0.0.1:8780/ です。Mac にログインするたびに自動で用意され、止まっても5分以内に起動し直します。"
fi
[ "$SKIP_DEPS" = 1 ] && echo "確かめていないこと: 仮想環境・部品・専用ブラウザ（--skip-deps のため）。"
echo "確かめていないこと: Claude の画面から AIまるなげ営業が見えるか。次の手順で確かめます。"
echo "1. Claude を完全に終了します（Dock の Claude を右クリックして「終了」）"
echo "2. Claude を開き直して、「AIまるなげ営業の状態を見て」と話しかけます。返事が来れば使えます"
[ -n "$PROFILE_NOTE" ] && { echo ""; echo "$PROFILE_NOTE"; }
echo ""
exit 0
