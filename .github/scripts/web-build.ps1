#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][string]$ContextPath)
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$ctx=Get-Content -LiteralPath $ContextPath -Raw|ConvertFrom-Json
$repo=[IO.Path]::GetFullPath([string]$ctx.repo_root)
$root=[IO.Path]::GetFullPath([string]$ctx.androidbuild_root)
$basePython=Join-Path $root 'Tools\PythonFull\Current\python\python.exe'
$node=Join-Path $root 'Tools\Node\node-v24.19.0-win-x64\node.exe'
$npmCli=Join-Path $root 'Tools\Node\node-v24.19.0-win-x64\node_modules\npm\bin\npm-cli.js'
$venvPython=Join-Path $repo '.venv\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $venvPython -PathType Leaf)){
  & $basePython -m venv (Join-Path $repo '.venv')
  if($LASTEXITCODE -ne 0){throw 'ANDROID_AGENT_BUILD=FAIL python_venv'}
}
if(-not(Test-Path -LiteralPath $venvPython -PathType Leaf)){throw 'ANDROID_AGENT_BUILD=FAIL python_venv_missing'}
& $venvPython -m pip install --disable-pip-version-check --no-input -r (Join-Path $repo 'requirements.lock')
if($LASTEXITCODE -ne 0){throw 'ANDROID_AGENT_BUILD=FAIL python_locked_dependencies'}
& $venvPython -m pip install --disable-pip-version-check --no-input --no-deps -e $repo
if($LASTEXITCODE -ne 0){throw 'ANDROID_AGENT_BUILD=FAIL python_package'}
$npmCache=Join-Path ([string]$ctx.work_root) 'npm-cache'
New-Item -ItemType Directory -Force -Path $npmCache|Out-Null
$env:NPM_CONFIG_CACHE=$npmCache
$frontend=Join-Path $repo 'frontend'
Push-Location $frontend
try{
  & $node $npmCli ci --no-audit --no-fund --cache $npmCache
  if($LASTEXITCODE -ne 0){throw 'ANDROID_AGENT_BUILD=FAIL npm_ci'}
  & $node $npmCli run build
  if($LASTEXITCODE -ne 0){throw 'ANDROID_AGENT_BUILD=FAIL frontend_build'}
}finally{Pop-Location}
if(-not(Test-Path -LiteralPath (Join-Path $frontend 'dist\index.html') -PathType Leaf)){throw 'ANDROID_AGENT_BUILD=FAIL dashboard_artifact_missing'}
Write-Output 'ANDROID_AGENT_BUILD=PASS python=locked npm=ci dashboard=dist'
