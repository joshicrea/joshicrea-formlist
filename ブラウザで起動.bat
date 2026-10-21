@echo off
cd /d "%~dp0"
rem Opens the screen of AI Marunage Eigyo.
rem install.ps1 -Open finds the screen already running for this folder, or starts one on a free port (8780-8782).
rem Messages from install.ps1 are in Japanese. This file uses English only, because cmd misreads Japanese in .bat files.
if not exist "%~dp0install.ps1" goto direct
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Open
if %errorlevel% EQU 0 exit /b 0
if %errorlevel% EQU 3 goto stop
goto direct

:stop
pause
exit /b 1

:direct
rem PowerShell could not run (for example, blocked by company settings). Start the screen directly on port 8780.
where pyw >nul 2>&1 && (start "" pyw -3 -X utf8 app.py & exit /b 0)
where pythonw >nul 2>&1 && (start "" pythonw -X utf8 app.py & exit /b 0)
where py >nul 2>&1 && (start "" py -3 -X utf8 app.py & exit /b 0)
where python >nul 2>&1 && (start "" python -X utf8 app.py & exit /b 0)
echo Python was not found on this PC.
echo Open the guide in this folder (the file ending with .md) and do section 1 again.
pause
exit /b 1
