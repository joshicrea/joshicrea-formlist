@echo off
rem AI Marunage Eigyo: setup by double-click (Windows). Opens the screen when it is done.
rem This file uses English only, because cmd misreads Japanese in .bat files. The Japanese messages come from install.ps1.
rem It downloads install.ps1 from the public repository and runs it with standard PowerShell. No other parts are needed.
rem Running it again updates the tool. Sender, messages, NG list and lists are kept.
rem install.ps1 stops with exit code 1 when no Claude app is found. Files are already in place by then, so this file still opens the screen.
title AI Marunage Eigyo setup
echo.
echo AI Marunage Eigyo: setup
echo It takes 10 to 20 minutes the first time. Do not close this window.
echo English text may appear. You do not need to understand it.
echo Wait until the Japanese text says the setup is finished, and the screen opens in your browser.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}; $f = Join-Path $env:TEMP 'formlist_install.ps1'; Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue; Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/joshicrea/joshicrea-formlist/master/install.ps1' -OutFile $f -ErrorAction Stop; & powershell -NoProfile -ExecutionPolicy Bypass -File $f; $rc = $LASTEXITCODE; $ins = Join-Path $env:USERPROFILE '.claude\formlist\app\install.ps1'; if (Test-Path -LiteralPath $ins) { & powershell -NoProfile -ExecutionPolicy Bypass -File $ins -Open; if ($LASTEXITCODE -ne 0 -and $rc -eq 0) { $rc = $LASTEXITCODE } }; exit $rc"
set "RC=%errorlevel%"
if not "%RC%"=="0" goto notice
echo.
echo Done. If the screen did not open, double-click the AI Marunage Eigyo icon on the desktop.
goto end
:notice
echo.
echo Finished with a notice. If the AI Marunage Eigyo screen opened in your browser, you can use it.
echo If no screen opened, and Japanese text above says the setup could not finish, copy that text and send it to the person who gave you this tool.
:end
echo.
pause
exit /b %RC%
