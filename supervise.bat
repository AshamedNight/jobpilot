@echo off
REM ---------------------------------------------------------------------------
REM jobpilot supervisor (closure 5)
REM
REM This script runs ONCE and exits. Windows Task Scheduler triggers it every
REM 5 minutes. Do NOT turn it into a long-running daemon: a resident supervisor
REM also dies (closed window / OOM / bluescreen), and a dead supervisor never
REM tells you it died. A fresh process every 5 minutes self-heals regardless.
REM
REM What it does:
REM   1. probe /api/status
REM   2. unreachable -> launch service + start pacer
REM   3. reachable but pacer stopped -> POST /api/pacing/start (idempotent)
REM   4. halted / paused -> do nothing (halt is protection, not a fault)
REM
REM Exit code: 0 = nothing to do or healed; 1 = failed to launch
REM
REM NOTE: keep this file pure ASCII. Chinese comments in a .bat saved as UTF-8
REM break under the GBK console codepage (cmd reports
REM "系统找不到指定的路径" and silently skips lines).
REM ---------------------------------------------------------------------------

cd /d "%~dp0"

REM Keep the log readable: python writes UTF-8 regardless of console codepage.
set PYTHONIOENCODING=utf-8

set "LOG=%~dp0data\supervise.log"

if not exist ".venv2\Scripts\python.exe" (
    echo [%date% %time%] venv missing, run 一键启动.bat first>>"%LOG%"
    exit /b 1
)

".venv2\Scripts\python.exe" -m app.tools.supervise --port 8010>>"%LOG%" 2>&1
exit /b %ERRORLEVEL%
