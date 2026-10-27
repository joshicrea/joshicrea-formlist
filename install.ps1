# AIまるなげ営業 インストールスクリプト（実行環境: Windows / PowerShell 5.1 以上）
# 利用者向けの手順は 導入と使い方.md。PowerShell で次の1行を実行する（Claude Code なら「次の1行を PowerShell で実行して」と頼む）:
#   $f = Join-Path $env:TEMP 'formlist_install.ps1'; Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/joshicrea/joshicrea-formlist/master/install.ps1' -OutFile $f; powershell -NoProfile -ExecutionPolicy Bypass -File $f
#
# やること: Python を確認（無ければ入れる）→ ツール本体を ~/.claude/formlist/app に置く → 本人専用の設定を取り込む
#           → 部品と専用ブラウザを入れて、読み込めるか確かめる → Claude（デスクトップ版・Claude Code・Codex のうち見つかったもの）に登録する
# もう一度実行すると更新になる。送信元・文面・送らないリスト・できたリストは消えない。
# 本人専用の設定: -ProfileZip <パス> で指定する。無ければ「ダウンロード」と「デスクトップ」から、
#   名前が「AIまるなげ営業_本人設定」で始まる zip（または展開したフォルダ）を探し、いちばん新しいものを使う。
# そのほかの指定:
#   -Uninstall  撤去。動いている画面を止め、登録を外し、本体を消す。設定と出力は残す
#   -Open       画面を開く（ブラウザで起動.bat から呼ぶ）。動いている画面があればそれを開き、無ければ空いている番号で起動する
#   検証用: -LocalSource <フォルダ> 手元から入れる / -TargetRoot <フォルダ> 置き場所を変える / -SkipDeps 部品の導入と確認を飛ばす
#           -DryRunRegister 登録と登録の取り外しを試走にする（Claude の設定を書き換えない。-Uninstall でも効く）
#           -ProfileSearchDir <フォルダ> 本人専用の設定を探す場所を変える
# 守備範囲: Claude の画面から実際にツールが見えるかは、このスクリプトでは確かめない（完了の表示にもそう出す）。
# 書き方の注意: PowerShell 5.1 では、関数や if 式から1要素の配列を返すと文字列に戻り、& で展開すると1文字ずつの引数に割れる。
#   Python に渡す引数は、必ず Invoke-Py -Rest @(...) で渡す（[string[]] で受けるので割れない）。
param([string]$LocalSource = "", [string]$TargetRoot = "", [switch]$SkipDeps, [switch]$DryRunRegister, [string]$ProfileUrl = "", [string]$ProfileZip = "", [string]$ProfileSearchDir = "", [switch]$Uninstall, [switch]$Open)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
# 出力の文字コードをそろえる(日本語版 Windows は既定が Shift_JIS で、Python の UTF-8 の出力と混ざって化ける)
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); $OutputEncoding = [Console]::OutputEncoding } catch {}
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$Repo = "joshicrea/joshicrea-formlist"
$Root = [IO.Path]::Combine($env:USERPROFILE, ".claude", "formlist")
if ($TargetRoot) { $Root = $TargetRoot }
$AppDir = [IO.Path]::Combine($Root, "app")
# 更新しても消さないもの（本人の設定と、これまでの結果）
$KeepNames = @("sender.json", "message_adoptions.json", "出力", "文面", "計測キー.txt")
$Ports = @(8780, 8781, 8782)
$ProfilePrefix = "AIまるなげ営業_本人設定"
$Manifest = ".install_files.txt"
$Ask = "表示をそのままコピーして、このツールをお渡しした担当者に送ってください。"
$script:Py = $null

function Find-Python {
    # 今のプロセスの PATH を捨てずに、入れたばかりの Python も見えるように前へ足す
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"  # 候補が標準エラーに書いても(Microsoft Store 誘導のスタブなど)、ここでは落とさない
    try {
        foreach ($c in @("py", "python")) {
            $cmd = Get-Command $c -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $cmd) { continue }
            try {
                if ($c -eq "py") { $out = & $cmd.Source -3 --version 2>&1 } else { $out = & $cmd.Source --version 2>&1 }
                if ($LASTEXITCODE -eq 0 -and "$out" -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 10) { return $cmd.Source }
            } catch {}
        }
    } finally { $ErrorActionPreference = $prev }
    return $null
}

# Python を呼ぶ。py ランチャーのときだけ -3 を付ける(python.exe に付けると Unknown option で失敗する)
function Invoke-Py([string[]]$Rest) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"  # 部品の導入が標準エラーに警告を書いても、ここでは落とさない。成否は終了コードで見る
    try {
        if ((Split-Path $script:Py -Leaf) -ieq "py.exe") { & $script:Py -3 @Rest } else { & $script:Py @Rest }
    } finally { $ErrorActionPreference = $prev }
}

function Write-NetError($err, $what) {
    $m = "$($err.Exception.Message)"
    Write-Host "$what できませんでした。"
    if ($m -match "407") { Write-Host "原因: 会社のプロキシが認証を求めています（407）。ネットワーク管理者に、github.com と pypi.org への接続を許可してもらってください。" }
    elseif ($m -match "SSL|TLS|証明書|certificate") { Write-Host "原因: 会社のネットワークの証明書の検査に止められています。ネットワーク管理者に、github.com と pypi.org を検査の対象外にしてもらってください。" }
    elseif ($m -match "名前|解決|resolve|remote name|ホスト") { Write-Host "原因: インターネットにつながっていないか、github.com の名前が引けません。ネットワークを確かめてください。" }
    else { Write-Host "原因: ネットワークの問題と思われます。つながっているか確かめて、もう一度実行してください。" }
    Write-Host "（元のメッセージ: $m）"
}

# ログイン時の自動起動と、デスクトップの起動用ショートカット。パソコンを再起動しても、画面が止まったままにならないようにする
# 試走(-DryRunRegister)のときは、置き場を FORMLIST_SHORTCUT_ROOT で指定しない限り、何も作らない
$StartupLnk = "AIまるなげ営業_自動起動.lnk"
$DesktopLnk = "AIまるなげ営業.lnk"
function Get-ShortcutDirs {
    if ($env:FORMLIST_SHORTCUT_ROOT) { return @{ Startup = [IO.Path]::Combine($env:FORMLIST_SHORTCUT_ROOT, "Startup"); Desktop = [IO.Path]::Combine($env:FORMLIST_SHORTCUT_ROOT, "Desktop") } }
    if ($DryRunRegister) { return $null }
    return @{ Startup = [Environment]::GetFolderPath("Startup"); Desktop = [Environment]::GetFolderPath("Desktop") }
}
function New-Lnk([string]$Path, [string]$Target, [string]$Arguments, [string]$Dir, [int]$Style, [string]$Desc) {
    New-Item -ItemType Directory -Force -Path (Split-Path $Path) | Out-Null
    $l = (New-Object -ComObject WScript.Shell).CreateShortcut($Path)
    $l.TargetPath = $Target; $l.Arguments = $Arguments; $l.WorkingDirectory = $Dir; $l.WindowStyle = $Style; $l.Description = $Desc
    $l.Save()
}
function Remove-Shortcuts {
    if ($env:FORMLIST_TASK_NAME) { try { Unregister-ScheduledTask -TaskName $env:FORMLIST_TASK_NAME -Confirm:$false -ErrorAction Stop } catch {} }
    elseif (-not $DryRunRegister) { Remove-ResidentTask }
    $d = Get-ShortcutDirs
    if (-not $d) { return }
    foreach ($p in @([IO.Path]::Combine($d.Startup, $StartupLnk), [IO.Path]::Combine($d.Desktop, $DesktopLnk))) { Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue }
}
$TaskName = "AIまるなげ営業_画面の常駐"
function Remove-ResidentTask {
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop } catch {}
}
# ログオンのたびに画面のサーバーを起動し、止まっていたら5分以内に起動し直す。1つだけ動く(app.py が、すでに動いていれば何もしない)
function Set-ResidentTask([string]$Exe) {
    $name = if ($env:FORMLIST_TASK_NAME) { $env:FORMLIST_TASK_NAME } else { $TaskName }
    try {
        $app = [IO.Path]::Combine($AppDir, "app.py")
        $act = New-ScheduledTaskAction -Execute $Exe -Argument "-X utf8 `"$app`" --no-browser" -WorkingDirectory $AppDir
        $trg = @((New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME))
        $rep = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
        $trg += $rep
        $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
        $pr = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $name -Action $act -Trigger $trg -Settings $set -Principal $pr -Description "AIまるなげ営業の画面を、パソコンを開いたときと、止まったときに自動で起動します（動いていれば何もしません）" -Force | Out-Null
        return $true
    } catch {
        Write-Host "常駐の登録（タスクスケジューラ）を作れませんでした（$($_.Exception.Message)）。スタートアップのショートカットだけで自動起動します。"
        return $false
    }
}
function Set-Shortcuts {
    $d = Get-ShortcutDirs
    if (-not $d) { Write-Host "（試走のため、ショートカットは作っていません）"; return $true }
    try {
        $exe = (@(Invoke-Py -Rest @("-c", "import sys; print(sys.executable)")) | Select-Object -Last 1).ToString().Trim()
        $w = [IO.Path]::Combine((Split-Path $exe), "pythonw.exe")
        if (Test-Path -LiteralPath $w) { $exe = $w }
        if (-not (Test-Path -LiteralPath $exe)) { throw "Python の場所を確かめられませんでした: $exe" }
        $app = [IO.Path]::Combine($AppDir, "app.py")
        New-Lnk ([IO.Path]::Combine($d.Startup, $StartupLnk)) $exe "-X utf8 `"$app`" --no-browser" $AppDir 7 "AIまるなげ営業の画面を、パソコンを開いたときに自動で起動します"
        $script:ResidentOk = if ($DryRunRegister -and -not $env:FORMLIST_TASK_NAME) { $false } else { Set-ResidentTask $exe }
        New-Lnk ([IO.Path]::Combine($d.Desktop, $DesktopLnk)) $exe "-X utf8 `"$app`"" $AppDir 7 "AIまるなげ営業の画面を開きます（止まっていれば起動します）"
        return $true
    } catch {
        Write-Host "自動起動のショートカットを作れませんでした（$($_.Exception.Message)）。画面が開かないときは、Claude に「営業ツールを起動して」と話しかけてください。"
        return $false
    }
}

function Test-SamePath([string]$a, [string]$b) {
    try { return ([IO.Path]::GetFullPath($a).TrimEnd("\") -ieq [IO.Path]::GetFullPath($b).TrimEnd("\")) } catch { return $false }
}

# 127.0.0.1 の画面サーバーに問い合わせる。会社のプロキシを通さない。3秒で打ち切る
function Invoke-Local([int]$Port, [string]$Path, [string]$Body = "", [string]$Token = "") {
    $req = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port$Path")
    $req.Proxy = $null; $req.Timeout = 3000; $req.ReadWriteTimeout = 3000
    if ($Body) {
        $req.Method = "POST"; $req.ContentType = "application/json"
        if ($Token) { $req.Headers["X-Token"] = $Token }
        $bytes = [Text.Encoding]::UTF8.GetBytes($Body); $req.ContentLength = $bytes.Length
        $s = $req.GetRequestStream(); $s.Write($bytes, 0, $bytes.Length); $s.Close()
    }
    $resp = $req.GetResponse()
    try { return (New-Object IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)).ReadToEnd() } finally { $resp.Close() }
}

# その番号で動いているこのツールの状態(tool・here・running・name)。このツールでなければ $null
function Get-ToolInfo([int]$Port) {
    try { $st = Invoke-Local $Port "/api/whoami" | ConvertFrom-Json; if ($st -and $st.tool -eq "formlist") { return $st } } catch {}
    # /api/whoami が無い古い版: トップページに埋め込まれたトークンで /api/job を読む。古い版を「別のアプリ」と取り違えない
    try {
        $page = Invoke-Local $Port "/"
        if ($page -match "const TOKEN='([^']+)'" -and $page.Contains("AIまるなげ営業")) {
            $req = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/api/job")
            $req.Proxy = $null; $req.Timeout = 3000; $req.Headers["X-Token"] = $Matches[1]
            $resp = $req.GetResponse()
            try { $j = (New-Object IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)).ReadToEnd() | ConvertFrom-Json } finally { $resp.Close() }
            if ($j -and $null -ne $j.lines) { return [pscustomobject]@{ tool = "formlist"; here = $j.here; running = [bool]$j.running; name = $j.name; port = $Port } }
        }
    } catch {}
    return $null
}

# その番号で、別のフォルダに入れたこのツールが動いているときの状態を返す(自分のフォルダでなければ $null)
function Get-OtherCopy([int]$Port, [string]$Dir) {
    $st = Get-ToolInfo $Port
    if ($st -and $st.here -and -not (Test-SamePath $st.here $Dir)) { return $st }
    return $null
}

# その番号で動いているのが、このフォルダのツールの画面なら、状態を返す。違えば $null
function Get-ToolState([int]$Port, [string]$Dir) {
    $st = Get-ToolInfo $Port
    if ($st -and $st.here -and (Test-SamePath $st.here $Dir)) { return $st }
    return $null
}

function Test-PortFree([int]$Port) {
    $c = New-Object Net.Sockets.TcpClient
    try { $iar = $c.BeginConnect("127.0.0.1", $Port, $null, $null); if ($iar.AsyncWaitHandle.WaitOne(500) -and $c.Connected) { return $false } } catch {} finally { $c.Close() }
    $l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Port)
    try { $l.Start(); return $true } catch { return $false } finally { try { $l.Stop() } catch {} }
}

function Get-ProcessTree([int]$Id) {
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $ids = New-Object System.Collections.Generic.List[int]
    $ids.Add($Id)
    for ($i = 0; $i -lt $ids.Count; $i++) { foreach ($p in $all) { if ($p.ParentProcessId -eq $ids[$i] -and -not $ids.Contains([int]$p.ProcessId)) { $ids.Add([int]$p.ProcessId) } } }
    return , $ids.ToArray()
}

# このフォルダの画面サーバーを止める。本番の送信中なら止めずに $false を返す
function Stop-ToolServers([string]$Dir, [switch]$RefuseIfBusy) {
    foreach ($p in $Ports) {
        $st = Get-ToolState $p $Dir
        if (-not $st) { continue }
        if ($st.running -and ($RefuseIfBusy -or $st.name -eq "本番送信")) { Write-Host "画面（$p 番）で「$($st.name)」が動いています。終わってから、もう一度実行してください。"; return $false }
        $tree = @()
        try { foreach ($o in @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $p -State Listen -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique)) { $tree += Get-ProcessTree $o } } catch {}
        try {
            $page = Invoke-Local $p "/"
            if ($page -match "const TOKEN='([^']+)'") { $null = Invoke-Local $p "/api/quit" "{}" $Matches[1] }
        } catch {}
        Start-Sleep -Seconds 2
        foreach ($id in $tree) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
        Write-Host "動いていた画面（$p 番）を止めました"
    }
    return $true
}

function Get-DownloadsDir {
    try {
        $v = (Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" -ErrorAction Stop)."{374DE290-123F-4565-9164-39C4925E467B}"
        if ($v) { return [Environment]::ExpandEnvironmentVariables($v) }
    } catch {}
    return [IO.Path]::Combine($env:USERPROFILE, "Downloads")
}

# 本人専用の設定を探す。zip か、展開したフォルダ（中に sender.json がある）。いちばん新しいもの
function Find-ProfileSource {
    $dirs = @()
    if ($ProfileSearchDir) { $dirs += $ProfileSearchDir } else { $dirs += Get-DownloadsDir; $dirs += [Environment]::GetFolderPath("Desktop") }
    $hits = @()
    foreach ($d in $dirs) {
        if (-not $d -or -not (Test-Path -LiteralPath $d)) { continue }
        $hits += @(Get-ChildItem -LiteralPath $d -Filter "$ProfilePrefix*.zip" -File -ErrorAction SilentlyContinue)
        foreach ($f in @(Get-ChildItem -LiteralPath $d -Filter "$ProfilePrefix*" -Directory -ErrorAction SilentlyContinue)) {
            if (Get-ChildItem -LiteralPath $f.FullName -Filter "sender.json" -File -Recurse -Depth 1 -ErrorAction SilentlyContinue) { $hits += $f }
        }
    }
    if ($hits.Count -eq 0) { return "" }
    return ($hits | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}

# sender.json が見本のまま（本人がまだ書いていない）か
function Test-SampleSender([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    $sample = [IO.Path]::Combine($AppDir, "sender.sample.json")
    if ((Test-Path $sample) -and ((Get-FileHash -LiteralPath $Path).Hash -eq (Get-FileHash -LiteralPath $sample).Hash)) { return $true }
    try { $j = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json; return ("$($j.'氏名')".StartsWith("【") -or -not "$($j.'氏名')") } catch { return $false }
}

# --- 画面を開く (-Open)。終了コード 0=開いた / 3=原因を表示して止まった ---
if ($Open) {
    $Here = $PSScriptRoot
    foreach ($p in $Ports) {
        if (Get-ToolState $p $Here) { Write-Host "画面を開きます: http://127.0.0.1:$p/"; Start-Process "http://127.0.0.1:$p/"; exit 0 }
    }
    # 画面のアドレスは 8780 番で固定。別のフォルダのこのツール(古いコピー)が居座っていれば、何も動いていないときだけ止めて入れ替える
    $others = @($Ports | ForEach-Object { Get-OtherCopy $_ $Here } | Where-Object { $_ })
    foreach ($o in $others) {
        if ($o.running) { Write-Host "別のフォルダのこのツールが、$($o.port)番で「$($o.name)」を実行中です。終わってから、もう一度開いてください。"; exit 3 }
    }
    foreach ($h in @($others | ForEach-Object { $_.here } | Select-Object -Unique)) { $null = Stop-ToolServers $h -RefuseIfBusy }
    $free = 0
    foreach ($p in $Ports) { if (Test-PortFree $p) { $free = $p; break } }
    if (-not $free) { Write-Host "8780〜8782 番が、すべてほかのアプリに使われています。使っていないアプリを閉じるか、パソコンを再起動してから、もう一度開いてください。"; exit 3 }
    $script:Py = Find-Python
    if (-not $script:Py) { Write-Host "Python が見つかりません。導入と使い方の「1 導入」を、もう一度行ってください。"; exit 3 }
    $exe = "$(Invoke-Py -Rest @('-c', 'import sys; print(sys.executable)') | Select-Object -Last 1)".Trim()
    $pyw = [IO.Path]::Combine((Split-Path $exe), "pythonw.exe")
    if (-not (Test-Path $pyw)) { $pyw = $exe }
    $logDir = [IO.Path]::Combine($Here, "出力")
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $errLog = [IO.Path]::Combine($logDir, "画面_起動エラー.txt")
    $env:FORMLIST_PORT = "$free"
    Start-Process -FilePath $pyw -ArgumentList @("-X", "utf8", "app.py") -WorkingDirectory $Here -RedirectStandardOutput ([IO.Path]::Combine($logDir, "画面_起動ログ.txt")) -RedirectStandardError $errLog -WindowStyle Hidden
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Get-ToolState $free $Here) { Write-Host "画面を開きます: http://127.0.0.1:$free/（自動で開かないときは、このアドレスをブラウザに入れます）"; exit 0 }
    }
    Write-Host "画面を起動できませんでした。$Ask"
    if (Test-Path $errLog) { Write-Host "（元のメッセージ）"; Get-Content -LiteralPath $errLog -Encoding UTF8 -Tail 15 | Out-Host }
    exit 3
}

Write-Host ""
if ($Uninstall) { Write-Host "AIまるなげ営業を撤去しています..." } else { Write-Host "AIまるなげ営業をインストールしています..." }
Write-Host ""

# --- 撤去 (-Uninstall) ---
# 動いている画面を止め、登録を外し、本体を消す。送信の記録・設定・できたリスト(出力)は、消さずに残す
if ($Uninstall) {
    $bad = 0
    if (-not (Stop-ToolServers $AppDir)) { exit 1 }
    $script:Py = Find-Python
    Remove-Shortcuts
    $reg = [IO.Path]::Combine($AppDir, "mcp_register.py")
    if (-not $script:Py) {
        Write-Host "Python が見つからないので、Claude の登録を自動では外せません。Claude の設定の「開発者」(Developer) から formlist を削除してください。"; $bad = 1
    } elseif (Test-Path $reg) {
        $rest = @("-X", "utf8", "mcp_register.py", "--remove")
        if ($DryRunRegister) { $rest += "--dry-run" }
        Push-Location $AppDir
        try { Invoke-Py -Rest $rest | Out-Host; if ($LASTEXITCODE -ne 0) { $bad = 1 } } finally { Pop-Location }
    } else { Write-Host "本体が見つかりません: $AppDir（すでに撤去済みかもしれません）" }
    # 残っているプロセス(Claude が起動した mcp_server.py・送る処理など、コマンドにこのフォルダを含むもの)を止める
    try {
        Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $_.Name -match "^pythonw?[\d.]*\.exe$|^pyw?\.exe$" -and $_.CommandLine -and $_.CommandLine.Contains($AppDir) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    } catch { Write-Host "動いている処理を自動では止められませんでした。パソコンを再起動すると止まります。" }
    $keep = [IO.Path]::Combine($Root, "撤去前の設定と出力_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
    if (Test-Path $AppDir) {
        Set-Location ([IO.Path]::GetTempPath())  # 消すフォルダの中にいると消せない
        New-Item -ItemType Directory -Force -Path $keep | Out-Null
        foreach ($n in $KeepNames) { $src = [IO.Path]::Combine($AppDir, $n); if (Test-Path $src) { Copy-Item $src $keep -Recurse -Force } }
        Start-Sleep -Seconds 1
        Remove-Item $AppDir -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $AppDir) { Write-Host "本体のフォルダの一部を消せませんでした（使用中の可能性）。パソコンを再起動してから、このフォルダを手で消してください: $AppDir"; $bad = 1 }
        else { Write-Host "本体を消しました。設定と出力は、ここに残してあります: $keep" }
    }
    Write-Host "送信の記録（送らない会社・送信済みの会社）は、残してあります: $env:LOCALAPPDATA\FormListTool"
    if ($DryRunRegister) { Write-Host "（試走のため、Claude の登録は外していません）" }
    if ($bad) { Write-Host "撤去の途中で、うまくいかなかったところがあります。上の表示を見てください。$Ask"; exit 1 }
    Write-Host "撤去が終わりました。Claude を完全に終了して開き直すと、AIまるなげ営業が消えています。"
    exit 0
}

# --- 1. Python ---
$script:Py = Find-Python
if (-not $script:Py) {
    Write-Host "Python が見つからないので、入れます（数分かかります）..."
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "winget が使えません。https://www.python.org/downloads/windows/ から Python を入れて、「Add python.exe to PATH」にチェックを入れてから、もう一度この手順を実行してください。"
        exit 1
    }
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements | Out-Host
    $wingetCode = $LASTEXITCODE
    $script:Py = Find-Python
    if (-not $script:Py) {
        if ($wingetCode -ne 0) {
            Write-Host "Python を自動で入れられませんでした（winget の終了コード $wingetCode）。会社の設定や、権限、ネットワークで止められている可能性があります。"
            Write-Host "https://www.python.org/downloads/windows/ から Python を入れて、「Add python.exe to PATH」にチェックを入れてから、同じ手順をもう一度実行してください。"
        } else {
            Write-Host "Python を入れましたが、まだ見つかりません。この画面（Claude Code から実行したときは Claude Code）を一度閉じて開き直し、同じ手順をもう一度実行してください。"
        }
        exit 1
    }
}
Write-Host "Python: $script:Py"

# --- 2. ツール本体を取得 ---
$Work = [IO.Path]::Combine([IO.Path]::GetTempPath(), "formlist-install-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$profileNote = ""
try {
    if ($LocalSource) {
        if (-not (Test-Path ([IO.Path]::Combine($LocalSource, "mcp_server.py")))) { Write-Host "LocalSource に mcp_server.py がありません: $LocalSource"; exit 1 }
        $Src = $LocalSource
        Write-Host "手元のフォルダから入れます: $Src"
    } else {
        $zip = [IO.Path]::Combine($Work, "src.zip")
        try { Invoke-WebRequest -Uri "https://github.com/$Repo/archive/refs/heads/master.zip" -OutFile $zip -UseBasicParsing } catch { Write-NetError $_ "GitHub からツール本体を取得"; exit 1 }
        Expand-Archive -Path $zip -DestinationPath $Work -Force
        $Src = (Get-ChildItem $Work -Directory | Where-Object { $_.Name -like "joshicrea-formlist-*" } | Select-Object -First 1).FullName
        if (-not $Src -or -not (Test-Path ([IO.Path]::Combine($Src, "mcp_server.py")))) { Write-Host "取得したものの中に mcp_server.py がありません。何も置いていません。"; exit 1 }
    }

    # --- 3. 配置（本人の設定と結果は残す。前の版にあって今の版に無いファイルは消す） ---
    if (-not (Stop-ToolServers $AppDir -RefuseIfBusy)) { exit 1 }  # 古い版の画面が動いたままだと、入れ替えた中身が使われない
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    $items = @(Get-ChildItem $Src -Force | Where-Object { -not $_.Name.StartsWith(".") -and $_.Name -ne "__pycache__" })
    $names = @($items | ForEach-Object { $_.Name })
    $old = [IO.Path]::Combine($AppDir, $Manifest)
    if (Test-Path $old) {
        foreach ($n in @(Get-Content -LiteralPath $old -Encoding UTF8)) {
            if ($n -and ($names -notcontains $n) -and ($KeepNames -notcontains $n) -and -not $n.StartsWith(".")) { Remove-Item -LiteralPath ([IO.Path]::Combine($AppDir, $n)) -Recurse -Force -ErrorAction SilentlyContinue }
        }
    }
    foreach ($it in $items) {
        $dest = [IO.Path]::Combine($AppDir, $it.Name)
        if (($KeepNames -contains $it.Name) -and (Test-Path $dest)) { continue }  # すでにあれば上書きしない
        if ($it.PSIsContainer) {
            if (Test-Path $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }  # ひな形などは、前の版の中身を残さず入れ直す
            Copy-Item -LiteralPath $it.FullName -Destination $dest -Recurse -Force
        } else { Copy-Item -LiteralPath $it.FullName -Destination $dest -Force }
    }
    Set-Content -LiteralPath $old -Value $names -Encoding UTF8
    $senderPath = [IO.Path]::Combine($AppDir, "sender.json")
    $sample = [IO.Path]::Combine($AppDir, "sender.sample.json")
    if (-not (Test-Path $senderPath) -and (Test-Path $sample)) { Copy-Item $sample $senderPath }  # 最初は、送信元のひな形から始める
    New-Item -ItemType Directory -Force -Path ([IO.Path]::Combine($AppDir, "出力")) | Out-Null

    # 本人専用の設定（送信元・文面・送らないリスト）。本人が直したものは上書きしない
    $prof = ""
    if ($ProfileUrl) {
        $prof = [IO.Path]::Combine($Work, "profile.zip")
        try { Invoke-WebRequest -Uri $ProfileUrl -OutFile $prof -UseBasicParsing } catch { Write-NetError $_ "設定ファイルを取得"; exit 1 }
    } elseif ($ProfileZip) {
        if (-not (Test-Path -LiteralPath $ProfileZip)) { Write-Host "設定ファイルが見つかりません: $ProfileZip"; exit 1 }
        $prof = $ProfileZip
    } else { $prof = Find-ProfileSource }
    if ($prof) {
        $profItem = Get-Item -LiteralPath $prof
        Write-Host "本人専用の設定を使います: $($profItem.FullName)（更新日時: $($profItem.LastWriteTime.ToString('yyyy-MM-dd HH:mm'))）"
        $pd = $prof
        if (-not $profItem.PSIsContainer) {
            # 展開する前に、中の名前を見る。上の階層へ出る名前・ドライブ名つきの名前があれば、何も取り込まない
            try {
                Add-Type -AssemblyName System.IO.Compression.FileSystem
                $za = [IO.Compression.ZipFile]::OpenRead($profItem.FullName)
                try { $badEntries = @($za.Entries | Where-Object { $_.FullName -match '\.\.|:' -or $_.FullName.StartsWith('/') -or $_.FullName.StartsWith('\') } | ForEach-Object { $_.FullName }) } finally { $za.Dispose() }
            } catch { Write-Host "設定ファイル（zip）を開けませんでした。ファイルが壊れているか、途中までしか保存されていません。もう一度保存し直してから実行してください。（元のメッセージ: $($_.Exception.Message)）"; exit 1 }
            if ($badEntries.Count -gt 0) { Write-Host "設定ファイル（zip）の中に、使えない名前があります（$($badEntries -join '、')）。取り込みません。$Ask"; exit 1 }
            $pd = [IO.Path]::Combine($Work, "profile")
            $pz = [IO.Path]::Combine($Work, "profile_copy.zip")
            Copy-Item -LiteralPath $prof -Destination $pz -Force  # 名前に日本語や空白があっても、展開で迷わない
            try { Expand-Archive -LiteralPath $pz -DestinationPath $pd -Force } catch { Write-Host "設定ファイル（zip）を開けませんでした。ファイルが壊れているか、途中までしか保存されていません。もう一度保存し直してから実行してください。（元のメッセージ: $($_.Exception.Message)）"; exit 1 }
        }
        $proot = Get-Item -LiteralPath $pd
        if (-not (Test-Path ([IO.Path]::Combine($proot.FullName, "sender.json")))) {
            $inner = Get-ChildItem -LiteralPath $proot.FullName -Directory | Where-Object { Test-Path ([IO.Path]::Combine($_.FullName, "sender.json")) } | Select-Object -First 1
            if ($inner) { $proot = $inner }
        }
        if (-not (Test-Path ([IO.Path]::Combine($proot.FullName, "sender.json")))) { Write-Host "設定ファイルの中に sender.json がありません。取り込みません。$Ask"; exit 1 }
        # 写してよいのは make_public_tree.py の PROFILE_ITEMS と同じ4つだけ(sender.json / message_adoptions.json / 文面の *.txt / 出力/NGリスト.txt)。
        # それ以外(.py など)を写すと、ツールのフォルダで Python の部品をすり替えられるので、名前で絞る
        $added = 0
        $skipped = @()
        foreach ($it in @(Get-ChildItem -LiteralPath $proot.FullName -Force)) {
            $dest = [IO.Path]::Combine($AppDir, $it.Name)
            if ($it.Name -eq "sender.json" -and -not $it.PSIsContainer) {
                if (Test-SampleSender $dest) { Copy-Item -LiteralPath $it.FullName -Destination $dest -Force; $added++ }
            } elseif ($it.Name -eq "message_adoptions.json" -and -not $it.PSIsContainer) {
                if (-not (Test-Path $dest)) { Copy-Item -LiteralPath $it.FullName -Destination $dest; $added++ }
            } elseif ($it.Name -eq "文面" -and $it.PSIsContainer) {
                New-Item -ItemType Directory -Force -Path $dest | Out-Null
                foreach ($f in @(Get-ChildItem -LiteralPath $it.FullName -Force)) {
                    if ($f.PSIsContainer -or $f.Extension -ine ".txt" -or $f.Name -match '\.\.|:') { $skipped += "文面\$($f.Name)"; continue }
                    $to = [IO.Path]::Combine($dest, $f.Name)
                    # 本人がまだ直していない(無い、またはひな形と同じ)ときだけ入れる
                    $tpl = [IO.Path]::Combine($AppDir, "文面ひな形", $f.Name)
                    $isTpl = (Test-Path $to) -and (Test-Path $tpl) -and ((Get-FileHash -LiteralPath $to).Hash -eq (Get-FileHash -LiteralPath $tpl).Hash)
                    if (-not (Test-Path $to) -or $isTpl) { Copy-Item -LiteralPath $f.FullName -Destination $to -Force; $added++ }
                }
            } elseif ($it.Name -eq "出力" -and $it.PSIsContainer) {
                foreach ($f in @(Get-ChildItem -LiteralPath $it.FullName -Force)) {
                    if ($f.Name -ne "NGリスト.txt" -or $f.PSIsContainer) { $skipped += "出力\$($f.Name)"; continue }
                    $to = [IO.Path]::Combine($dest, $f.Name)
                    New-Item -ItemType Directory -Force -Path $dest | Out-Null
                    if (-not (Test-Path $to)) { Copy-Item -LiteralPath $f.FullName -Destination $to; $added++ }
                }
            } else { $skipped += $it.Name }
        }
        if ($skipped.Count -gt 0) { Write-Host "設定ではないので、取り込まなかったもの: $($skipped -join '、')" }
        Write-Host "本人専用の設定を取り込みました（新しく入れたファイル: $added。すでに本人が直していたものは、そのままです）"
    }
    if (Test-SampleSender $senderPath) {
        $profileNote = "送信元はまだ見本のままです。Claude Code に、担当者から届いた「設定の文」を貼って送ると、送信元と文面が入ります（導入と使い方の「2 最初の1回だけ」）。"
    }
    Write-Host "配置しました: $AppDir"
} finally {
    Remove-Item $Work -Recurse -Force -ErrorAction SilentlyContinue
}

# --- 4. 部品と専用ブラウザ。入れたあとに、読み込めるか確かめる ---
$checked = ""
if (-not $SkipDeps) {
    Write-Host "部品を入れています（数分かかります）..."
    Invoke-Py -Rest @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", "playwright", "requests", "beautifulsoup4", "openpyxl", "mcp<2", "pydantic") | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Host "部品の導入に失敗しました（終了コード $LASTEXITCODE）。上の表示に「CERTIFICATE_VERIFY_FAILED」があれば、会社のネットワークの証明書検査が原因です。「407」があればプロキシの認証が原因です。それ以外は、$Ask"; exit 1 }
    Write-Host "ホームページを開くための専用ブラウザを入れています（数分かかります）..."
    Invoke-Py -Rest @("-m", "playwright", "install", "chromium") | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Host "専用ブラウザの導入に失敗しました（終了コード $LASTEXITCODE）。上の表示に「self signed certificate」があれば、会社のネットワークの証明書検査が原因です。それ以外は、$Ask"; exit 1 }
    $code = "import importlib.util, os, sys; miss=[m for m in ('playwright','requests','bs4','openpyxl','mcp','pydantic') if importlib.util.find_spec(m) is None]; print('MISSING ' + ' '.join(miss)) if miss else None; sys.exit(2) if miss else None; from mcp.server.fastmcp import FastMCP; from playwright.sync_api import sync_playwright; pw=sync_playwright().start(); exe=pw.chromium.executable_path; pw.stop(); ok=os.path.exists(exe); print(('CHROMIUM ' if ok else 'NOCHROMIUM ') + exe); sys.exit(0 if ok else 3)"
    $res = @(Invoke-Py -Rest @("-X", "utf8", "-c", $code))
    $resCode = $LASTEXITCODE
    $res | Out-Host
    if ($resCode -ne 0) { Write-Host "部品か専用ブラウザが、入れたあとも読み込めませんでした（終了コード $resCode）。同じ手順をもう一度実行してください。それでも同じなら、$Ask"; exit 1 }
    $checked = "部品が読み込めること。専用ブラウザがあること。"
}

# --- 5. Claude への登録 ---
Write-Host "Claude に登録しています..."
$rest = @("-X", "utf8", "mcp_register.py")
if ($DryRunRegister) { $rest += "--dry-run" }
Push-Location $AppDir
try { Invoke-Py -Rest $rest | Out-Host; $regCode = $LASTEXITCODE } finally { Pop-Location }
if ($regCode -ne 0) { Write-Host "Claude への登録で問題が起きました（終了コード $regCode）。$Ask"; exit 1 }

$shortcutOk = Set-Shortcuts
Write-Host ""
Write-Host "インストールが終わりました。"
Write-Host "確かめたこと: $($checked)Claude への登録を書いたこと$(if ($DryRunRegister) { '（試走のため、実際には書いていません）' })。$(if ($shortcutOk -and -not $DryRunRegister) { '起動用のショートカットを作ったこと。' })"
if ($shortcutOk -and -not $DryRunRegister) {
    Write-Host "画面のアドレスは、いつも http://127.0.0.1:8780/ です。パソコンを開くたびに自動で用意され、止まっても5分以内に起動し直します（ショートカットを押す必要はありません）。"
    if (-not $script:ResidentOk) { Write-Host "ただし、常駐の登録（タスクスケジューラ）は作れませんでした。パソコンを開いたあとは、デスクトップの「AIまるなげ営業」を押すと開きます。" }
}
if ($SkipDeps) { Write-Host "確かめていないこと: 部品と専用ブラウザ（-SkipDeps のため）。" }
Write-Host "確かめていないこと: Claude の画面から AIまるなげ営業が見えるか。次の手順で確かめます。"
Write-Host "1. Claude を完全に終了します（画面右下の「^」の中にある Claude の絵を右クリックして「終了」）"
Write-Host "2. Claude を開き直して、「AIまるなげ営業の状態を見て」と話しかけます。返事が来れば使えます"
if ($profileNote) { Write-Host ""; Write-Host $profileNote }
Write-Host ""
exit 0
