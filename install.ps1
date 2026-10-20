# AIまるなげ営業 インストールスクリプト for Windows
# 対象: PowerShell 5.1 以上。Claude Code のチャットに次の1行を貼って実行する
#
#   以下のURLからAIまるなげ営業のインストールスクリプトを取得して、
#   内容を確認してから実行してください:
#   https://raw.githubusercontent.com/joshicrea/joshicrea-formlist/master/install.ps1
#
# やること: Python を確認（無ければ入れる）→ ツール本体を ~/.claude/formlist/app に置く → 部品と専用ブラウザを入れる
#           → Claude（デスクトップ版・Claude Code・Codex のうち見つかったもの）に登録する。
# もう一度実行すると更新になる。送信元の設定・文面・送らないリスト・できたリストは消えない。
# 検証用: -LocalSource <フォルダ> で手元のフォルダから入れる。-TargetRoot <フォルダ> で置き場所を変える。-SkipDeps で部品の導入を飛ばす。-DryRunRegister で登録を試走にする。-ProfileUrl <URL> で、本人専用の設定（送信元・文面）を取り込む。
param([string]$LocalSource = "", [string]$TargetRoot = "", [switch]$SkipDeps, [switch]$DryRunRegister, [string]$ProfileUrl = "")

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$Repo = "joshicrea/joshicrea-formlist"
$Root = if ($TargetRoot) { $TargetRoot } else { [IO.Path]::Combine($env:USERPROFILE, ".claude", "formlist") }
$AppDir = [IO.Path]::Combine($Root, "app")
# 更新しても消さないもの（本人の設定と、これまでの結果）
$KeepNames = @("sender.json", "message_adoptions.json", "出力", "文面")

Write-Host ""
Write-Host "AIまるなげ営業をインストールしています..."
Write-Host ""

function Find-Python {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    foreach ($c in @("py", "python")) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) {
            $verArgs = if ($c -eq "py") { @("-3", "--version") } else { @("--version") }
            $out = & $cmd.Source @verArgs 2>&1
            if ($LASTEXITCODE -eq 0 -and "$out" -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 9) { return $cmd.Source }
        }
    }
    return $null
}

# --- 1. Python ---
$Py = Find-Python
if (-not $Py) {
    Write-Host "Python が見つからないので、入れます（数分かかります）..."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "winget が使えません。https://www.python.org/downloads/windows/ から Python を入れて、「Add python.exe to PATH」にチェックを入れてから、もう一度この手順を実行してください。"
        exit 1
    }
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements | Out-Host
    $Py = Find-Python
    if (-not $Py) {
        Write-Host "Python を入れましたが、まだ見つかりません。Claude Code を一度閉じて開き直し、同じ手順をもう一度実行してください。"
        exit 1
    }
}
Write-Host "Python: $Py"

# --- 2. ツール本体を取得 ---
$Work = [IO.Path]::Combine([IO.Path]::GetTempPath(), "formlist-install-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $Work | Out-Null
try {
    if ($LocalSource) {
        if (-not (Test-Path ([IO.Path]::Combine($LocalSource, "mcp_server.py")))) { Write-Host "LocalSource に mcp_server.py がありません: $LocalSource"; exit 1 }
        $Src = $LocalSource
        Write-Host "手元のフォルダから入れます: $Src"
    } else {
        $zip = [IO.Path]::Combine($Work, "src.zip")
        Invoke-WebRequest -Uri "https://github.com/$Repo/archive/refs/heads/master.zip" -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $Work -Force
        $Src = (Get-ChildItem $Work -Directory | Where-Object { $_.Name -like "joshicrea-formlist-*" } | Select-Object -First 1).FullName
        if (-not $Src -or -not (Test-Path ([IO.Path]::Combine($Src, "mcp_server.py")))) { Write-Host "取得したものの中に mcp_server.py がありません。何も置いていません。"; exit 1 }
    }

    # --- 3. 配置（本人の設定と結果は残す） ---
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    Get-ChildItem $Src -Force | Where-Object { $_.Name -notin @(".git", ".github", "__pycache__") } | ForEach-Object {
        $dest = [IO.Path]::Combine($AppDir, $_.Name)
        if (($KeepNames -contains $_.Name) -and (Test-Path $dest)) { return }  # すでにあれば上書きしない
        if ($_.PSIsContainer) { Copy-Item -Path $_.FullName -Destination $dest -Recurse -Force } else { Copy-Item -Path $_.FullName -Destination $dest -Force }
    }
    $sender = [IO.Path]::Combine($AppDir, "sender.json")
    $sample = [IO.Path]::Combine($AppDir, "sender.sample.json")
    if (-not (Test-Path $sender) -and (Test-Path $sample)) { Copy-Item $sample $sender }  # 最初は、送信元のひな形から始める
    New-Item -ItemType Directory -Force -Path ([IO.Path]::Combine($AppDir, "出力")) | Out-Null

    # 本人専用の設定（送信元・文面・送らないリスト）。すでに本人の設定があれば、上書きしない
    if ($ProfileUrl) {
        $pz = [IO.Path]::Combine($Work, "profile.zip")
        $pd = [IO.Path]::Combine($Work, "profile")
        Invoke-WebRequest -Uri $ProfileUrl -OutFile $pz -UseBasicParsing
        Expand-Archive -Path $pz -DestinationPath $pd -Force
        $root = $pd
        $inner = Get-ChildItem $pd -Directory | Select-Object -First 1
        if ($inner -and -not (Test-Path ([IO.Path]::Combine($pd, "sender.json")))) { $root = $inner.FullName }
        if (-not (Test-Path ([IO.Path]::Combine($root, "sender.json")))) { Write-Host "設定ファイルの中に sender.json がありません。取り込みません。"; exit 1 }
        Get-ChildItem $root -Force | ForEach-Object {
            $dest = [IO.Path]::Combine($AppDir, $_.Name)
            if ($_.PSIsContainer) {
                New-Item -ItemType Directory -Force -Path $dest | Out-Null
                Get-ChildItem $_.FullName -Recurse -File | ForEach-Object {
                    $rel = $_.FullName.Substring($_.FullName.IndexOf($root) + $root.Length).TrimStart("\")
                    $to = [IO.Path]::Combine($AppDir, $rel)
                    New-Item -ItemType Directory -Force -Path (Split-Path $to) | Out-Null
                    if (-not (Test-Path $to)) { Copy-Item $_.FullName $to }
                }
            } elseif ($_.Name -eq "sender.json") {
                # ひな形から作っただけの sender.json は、本人専用の設定で置き換える。本人が直したものは残す
                $sample2 = [IO.Path]::Combine($AppDir, "sender.sample.json")
                if (-not (Test-Path $dest) -or ((Test-Path $sample2) -and ((Get-FileHash $dest).Hash -eq (Get-FileHash $sample2).Hash))) { Copy-Item $_.FullName $dest -Force }
            } elseif (-not (Test-Path $dest)) { Copy-Item $_.FullName $dest }
        }
        Write-Host "本人専用の設定を取り込みました"
    }
    Write-Host "配置しました: $AppDir"
} finally {
    Remove-Item $Work -Recurse -Force -ErrorAction SilentlyContinue
}

# --- 4. 部品と専用ブラウザ ---
if (-not $SkipDeps) {
    Write-Host "部品を入れています（数分かかります）..."
    $pyArgs = if ((Split-Path $Py -Leaf) -like "py*") { @("-3") } else { @() }
    & $Py @pyArgs -m pip install --quiet playwright requests beautifulsoup4 openpyxl mcp pydantic | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Host "部品の導入に失敗しました。上の表示を、そのままコピーして林さんに送ってください。"; exit 1 }
    Write-Host "ホームページを開くための専用ブラウザを入れています（数分かかります）..."
    & $Py @pyArgs -m playwright install chromium | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Host "専用ブラウザの導入に失敗しました。上の表示を、そのままコピーして林さんに送ってください。"; exit 1 }
}

# --- 5. Claude への登録 ---
Write-Host "Claude に登録しています..."
Push-Location $AppDir
try {
    $pyArgs = if ((Split-Path $Py -Leaf) -like "py*") { @("-3") } else { @() }
    $regArgs = if ($DryRunRegister) { @("--dry-run") } else { @() }
    & $Py @pyArgs -X utf8 mcp_register.py @regArgs | Out-Host
    $reg = $LASTEXITCODE
} finally { Pop-Location }
if ($reg -ne 0) { Write-Host "Claude への登録で問題が起きました。上の表示を、そのままコピーして林さんに送ってください。"; exit 1 }

Write-Host ""
Write-Host "インストールが終わりました。Claude を完全に終了して（画面右下のタスクトレイからも）開き直し、"
Write-Host "「AIまるなげ営業で、江東区のサロンを探して」と話しかけてください。"
Write-Host ""
