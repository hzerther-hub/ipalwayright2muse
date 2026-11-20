@echo off
REM ===========================================================================
REM  static exit IP panel launcher
REM  ASCII-only on purpose: PowerShell 5.1 reads .bat/.ps1 as ANSI, CJK would
REM  be mangled. The UI itself is Chinese; this file stays ASCII.
REM ===========================================================================
setlocal enabledelayedexpansion

set "ROOT=%~dp0.."
cd /d "%ROOT%"

if /i "%~1"=="stop" goto stop
if /i "%~1"=="status" goto status
if /i "%~1"=="smoke" goto smoke
goto start

REM ---------------------------------------------------------------- start
:start
echo.
echo ============================================================
echo   static exit IP panel
echo ============================================================
echo.
echo   URL: http://127.0.0.1:18611
echo.
echo   (This panel is a separate process on purpose: it must stay
echo    reachable while muse2api is down so it can restart it.)
echo.

REM start in the background, then verify
start "" /min python "%ROOT%\server.py"
timeout /t 3 /nobreak >nul
curl -s -o nul -w "panel HTTP %{http_code}\n" http://127.0.0.1:18611/api/status
goto :eof

REM ----------------------------------------------------------------- stop
:stop
echo --- stopping panel ---
for /f "tokens=2" %%p in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":18611"') do (
  echo killing %%p
  taskkill /PID %%p /F /T >nul 2>&1
)
goto :eof

REM --------------------------------------------------------------- status
:status
curl -s http://127.0.0.1:18611/api/status
echo.
goto :eof

REM ---------------------------------------------------------------- smoke
:smoke
python "%ROOT%\tools\_panel_smoke.py"
goto :eof
