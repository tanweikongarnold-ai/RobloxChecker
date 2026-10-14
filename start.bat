@echo off
setlocal
cd /d "%~dp0"
title RobloxChecker bot

rem ---- find Python ------------------------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :nopython

rem ---- first run: make a private Python folder and install what the bot needs
if not exist ".venv\Scripts\python.exe" (
  echo Setting up for the first time. This takes a minute or two...
  %PY% -m venv .venv
  if errorlevel 1 goto :failed
)
echo Checking everything is installed...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :failed

rem ---- run the bot (it asks for your token the first time) --------------
".venv\Scripts\python.exe" -m bot
echo.
echo The bot has stopped. Double-click start.bat to start it again.
pause
exit /b 0

:nopython
echo.
echo Python 3.10 or newer is needed and wasn't found.
echo   1. Go to https://www.python.org/downloads/ and download Python.
echo   2. When installing, TICK the box "Add python.exe to PATH".
echo   3. Then double-click start.bat again.
echo.
pause
exit /b 1

:failed
echo.
echo Something went wrong while setting up (see the messages above).
echo Check your internet connection and double-click start.bat again.
echo If it keeps happening, delete the ".venv" folder and try once more.
echo.
pause
exit /b 1
