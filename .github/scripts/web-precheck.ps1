#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][string]$ContextPath)
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$ctx=Get-Content -LiteralPath $ContextPath -Raw|ConvertFrom-Json
$repo=[IO.Path]::GetFullPath([string]$ctx.repo_root)
$androidBuildRoot=[IO.Path]::GetFullPath([string]$ctx.androidbuild_root)
$python=Join-Path $androidBuildRoot 'Tools\PythonFull\Current\python\python.exe'
$node=Join-Path $androidBuildRoot 'Tools\Node\node-v24.19.0-win-x64\node.exe'
$npmCli=Join-Path $androidBuildRoot 'Tools\Node\node-v24.19.0-win-x64\node_modules\npm\bin\npm-cli.js'
$required=@(
  '.androidbuild.json','requirements.lock','pyproject.toml','frontend\package-lock.json',
  'frontend\vite.config.ts','.github\scripts\web-build.ps1','.github\scripts\web-health.ps1',
  '.github\scripts\web-smoke.ps1','.github\scripts\web-functional.ps1','gitd\app.py','gitd\cli.py',
  'gitd\mcp_server.py','gitd\routers\phone.py',
  'gitd\integrations\android_sdk_environment.py','gitd\integrations\androidbuild_device_lease.py',
  'gitd\integrations\androidbuild_fastapi_lease.py'
)
foreach($relative in $required){if(-not(Test-Path -LiteralPath (Join-Path $repo $relative) -PathType Leaf)){throw "ANDROID_AGENT_PRECHECK=FAIL missing=$relative"}}
if(-not(Test-Path -LiteralPath $python -PathType Leaf)){throw 'ANDROID_AGENT_PRECHECK=FAIL androidbuild_python_missing'}
if(-not(Test-Path -LiteralPath $node -PathType Leaf)){throw 'ANDROID_AGENT_PRECHECK=FAIL androidbuild_node_24_19_missing'}
if(-not(Test-Path -LiteralPath $npmCli -PathType Leaf)){throw 'ANDROID_AGENT_PRECHECK=FAIL androidbuild_npm_missing'}
$expected=[string]$ctx.expected_sha
if($expected -notmatch '^[a-fA-F0-9]{40}$'){throw 'ANDROID_AGENT_PRECHECK=FAIL expected_sha_invalid'}
$git=Join-Path $androidBuildRoot 'Tools\Git\cmd\git.exe'
if(-not(Test-Path -LiteralPath $git -PathType Leaf)){$git=(Get-Command git.exe -ErrorAction Stop).Source}
$head=& $git -C $repo rev-parse HEAD
if($LASTEXITCODE -ne 0 -or ([string]$head).Trim() -cne $expected){throw 'ANDROID_AGENT_PRECHECK=FAIL source_sha_mismatch'}
$pyVersion=& $python -c 'import platform; print(platform.python_version())'
if($LASTEXITCODE -ne 0 -or ([string]$pyVersion).Trim() -cne '3.13.15'){throw 'ANDROID_AGENT_PRECHECK=FAIL python_version_mismatch'}
$nodeVersion=& $node --version
if($LASTEXITCODE -ne 0 -or ([string]$nodeVersion).Trim() -cne 'v24.19.0'){throw 'ANDROID_AGENT_PRECHECK=FAIL node_version_mismatch'}
$npmVersion=& $node $npmCli --version
if($LASTEXITCODE -ne 0 -or ([string]$npmVersion).Trim() -cne '11.17.0'){throw 'ANDROID_AGENT_PRECHECK=FAIL npm_version_mismatch'}
$profile=Get-Content -LiteralPath (Join-Path $repo '.androidbuild.json') -Raw|ConvertFrom-Json
if($profile.project.id -cne 'android-agent-workshop' -or $profile.project.kind -cne 'web-service' -or $profile.delivery.mode -cne 'web-validation'){throw 'ANDROID_AGENT_PRECHECK=FAIL core_profile_invalid'}
Write-Output 'ANDROID_AGENT_PRECHECK=PASS source_sha=PASS python=3.13.15 node=24.19.0 npm=11.17.0 locks=present devices_touched=false'
