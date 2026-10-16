@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3.12 -m venv .venv
  if errorlevel 1 goto fail
)
.venv\Scripts\python.exe -c "import cshogi" >nul 2>&1
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto fail
)
.venv\Scripts\python.exe -X utf8 app.py
if errorlevel 1 goto fail
exit /b 0
:fail
echo Python 3.12 and the error message above should be checked.
pause
exit /b 1
