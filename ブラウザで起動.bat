@echo off
cd /d "%~dp0"
where pyw >nul 2>&1 && (start "" pyw -3 -X utf8 app.py & goto :eof)
where pythonw >nul 2>&1 && (start "" pythonw -X utf8 app.py & goto :eof)
where py >nul 2>&1 && (start "" py -3 -X utf8 app.py & goto :eof)
where python >nul 2>&1 && (start "" python -X utf8 app.py & goto :eof)
echo Python was not found on this PC.
echo Install Python first. See Step 1-2 in the file: Setup and Usage (Dounyuu to Tsukaikata).
echo Remember to check Add python.exe to PATH.
pause
