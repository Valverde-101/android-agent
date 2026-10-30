#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][string]$ContextPath)
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$ctx=Get-Content -LiteralPath $ContextPath -Raw|ConvertFrom-Json
$repo=[IO.Path]::GetFullPath([string]$ctx.repo_root)
$python=Join-Path $repo '.venv\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $python -PathType Leaf) -or -not(Test-Path -LiteralPath (Join-Path $repo 'frontend\dist\index.html') -PathType Leaf)){
  & (Join-Path $PSScriptRoot 'web-build.ps1') -ContextPath $ContextPath
}
$env:HOST='127.0.0.1';$env:PORT='5055';$env:GITD_ENABLE_IOS='0'
$env:OPENAI_API_KEY='';$env:ANTHROPIC_API_KEY='';$env:OPENROUTER_API_KEY=''
$pythonCheck=Join-Path ([string]$ctx.work_root) 'android-agent-core-health.py'
$pythonSource=@'
from gitd.app import app
from gitd.config import settings
assert settings.ios_platform_enabled is False
assert any(route.path == "/api/health" for route in app.routes)
print("API import and health route: OK")
'@
Set-Content -LiteralPath $pythonCheck -Value $pythonSource -Encoding utf8
Push-Location $repo
try{
  & $python $pythonCheck
  if($LASTEXITCODE -ne 0){throw 'ANDROID_AGENT_HEALTH=FAIL python_api_import'}
}finally{Pop-Location;Remove-Item -LiteralPath $pythonCheck -Force -ErrorAction SilentlyContinue}
$index=Join-Path $repo 'frontend\dist\index.html'
if(-not(Test-Path -LiteralPath $index -PathType Leaf)){throw 'ANDROID_AGENT_HEALTH=FAIL dashboard_artifact_missing'}
$html=Get-Content -LiteralPath $index -Raw
if($html -notmatch '<div id="app"'){throw 'ANDROID_AGENT_HEALTH=FAIL dashboard_entry_missing'}
Write-Output 'ANDROID_AGENT_HEALTH=PASS api_import=PASS ios=disabled dashboard=present'
