@echo off
chcp 65001 >nul
rem ============================================================
rem  jobpilot 一键启动（Windows）
rem  第一次运行会自动建虚拟环境并装依赖，之后直接启动。
rem  界面：http://127.0.0.1:8010
rem ============================================================
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv2\Scripts\python.exe"

if not exist "%PY%" (
  echo [1/3] 首次运行：创建虚拟环境 ...
  python -m venv .venv2 || goto :fail
  echo [2/3] 安装依赖（可能需要几分钟）...
  "%PY%" -m pip install --upgrade pip -q
  "%PY%" -m pip install -r requirements.txt -q || goto :fail
)

if not exist "config.yaml" (
  if exist "config.example.yaml" (
    copy /y "config.example.yaml" "config.yaml" >nul
    echo [提示] 已生成 config.yaml，请先改里面的三处路径再启动：
    echo        accounts[].chrome_profile / data_dir / chrome_exe
    notepad "config.yaml"
    pause
  ) else (
    echo [错误] 找不到 config.example.yaml
    goto :fail
  )
)

echo [3/3] 启动服务 ...
"%PY%" -m app.tools.launch --detach --open
if errorlevel 1 goto :fail

echo.
echo 已启动。面板： http://127.0.0.1:8010
echo 停止： python -m app.tools.multi stop
pause
exit /b 0

:fail
echo.
echo [失败] 上面有错误信息。常见原因：
echo   1. 没装 Python 3.11+ 或没加到 PATH
echo   2. 没装 Chrome（自动化要驱动真实 Chrome）
echo   3. config.yaml 路径填错
pause
exit /b 1
