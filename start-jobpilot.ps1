# start-jobpilot.ps1 — jobpilot 唯一启动入口（单 worker，只绑回环）
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Py = Join-Path $Root '.venv2\Scripts\python.exe'

$env:JOBPILOT_DB              = Join-Path $Root 'data\jobpilot-new.db'
# 【2026-11-13 发布】原来写死某个用户的家目录，换台机器就不存在。改用当前用户的家目录。
$env:JOBPILOT_BOSS_CRED_DIR   = Join-Path $env:USERPROFILE '.boss-agent-jobpilot-new'
$env:JOBPILOT_BOSS_BUDGET_DIR = Join-Path $env:USERPROFILE '.boss-agent-test'
# 连接/登录功能才用，B 连接验收前把真值填进来：
$env:JOBPILOT_CHROME_EXE         = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$env:JOBPILOT_CHROME_PROFILE_DIR = Join-Path $env:USERPROFILE 'chrome-boss'

# 启动前防呆：配置被覆盖回默认时直接拒绝启动（这个坑踩过 4 次了）
$pre = & $Py -c "from app.config import load_config; c=load_config(); print(c['adapter']); print((c.get('target') or {}).get('search_url') or '')"
if ($pre[0] -ne 'boss') { throw "config.yaml 的 adapter=$($pre[0])，不是 boss —— 先复原再启动" }
if (-not $pre[1])       { throw "target.search_url 为空 —— 淄博会走 --city 触发 INVALID_PARAM，先补 URL" }

Write-Host "python : $Py"
Write-Host "db     : $env:JOBPILOT_DB"
& $Py -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1
