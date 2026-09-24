@echo off
cd /d "%~dp0"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 -m reddit_joiner %*
  exit /b %ERRORLEVEL%
)
where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python -m reddit_joiner %*
  exit /b %ERRORLEVEL%
)
echo Python was not found. Install Python 3, then run: py -3 -m pip install -r requirements.txt
exit /b 1
