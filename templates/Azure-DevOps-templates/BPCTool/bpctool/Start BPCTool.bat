@echo off
setlocal
cd /d "%~dp0"

echo === BPCTool ===
echo Preparing environment (first launch may take a minute)...
node scripts\ensure-env.mjs
if errorlevel 1 (
  echo Setup failed. Ensure Node.js 20.19+ or 22.12+ and Python 3.11+ are installed.
  pause
  exit /b 1
)

echo.
echo Starting BPCTool (UI + local runner)...
echo A browser window should open shortly. Leave this window open while you work.
echo Close this window to stop the app.
echo.

call npm start
pause
