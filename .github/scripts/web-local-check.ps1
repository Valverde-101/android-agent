#requires -Version 5.1
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$ContextPath,
  [Parameter(Mandatory)][ValidateSet('smoke','functional')][string]$Mode
)
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
$ctx=Get-Content -LiteralPath $ContextPath -Raw|ConvertFrom-Json
$repo=[IO.Path]::GetFullPath([string]$ctx.repo_root)
$root=[IO.Path]::GetFullPath([string]$ctx.androidbuild_root)
$python=Join-Path $repo '.venv\Scripts\python.exe'
$node=Join-Path $root 'Tools\Node\node-v24.19.0-win-x64\node.exe'
$vite=Join-Path $repo 'frontend\node_modules\vite\bin\vite.js'
function Get-FreeLoopbackPort {
  $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
  try{$listener.Start();return [int]$listener.LocalEndpoint.Port}finally{$listener.Stop()}
}
function Wait-HttpOk([string]$Url,[System.Diagnostics.Process]$Process){
  $until=(Get-Date).AddSeconds(25)
  do{
    if($Process.HasExited){throw 'ANDROID_AGENT_CHECK=FAIL local_service_exited'}
    try{$response=Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2;if($response.StatusCode -eq 200){return $response}}catch{}
    Start-Sleep -Milliseconds 250
  }while((Get-Date)-lt $until)
  throw 'ANDROID_AGENT_CHECK=FAIL local_service_timeout'
}
$apiPort=Get-FreeLoopbackPort
$apiPort2=Get-FreeLoopbackPort
while($apiPort2 -eq $apiPort){$apiPort2=Get-FreeLoopbackPort}
$db=Join-Path ([string]$ctx.work_root) ("android-agent-{0}-{1}.db" -f $Mode,[guid]::NewGuid().ToString('N'))
$saved=@{}
foreach($key in @('DB_PATH','HOST','PORT','PYTHONUTF8','GITD_ENABLE_IOS','GITD_CORS_ORIGINS','ANDROID_AGENT_API_TARGET','OPENAI_API_KEY','ANTHROPIC_API_KEY','OPENROUTER_API_KEY')){$saved[$key]=[Environment]::GetEnvironmentVariable($key,'Process')}
$api=$null;$ui=$null
try{
  $env:DB_PATH=$db;$env:HOST='127.0.0.1';$env:PORT=[string]$apiPort;$env:PYTHONUTF8='1';$env:GITD_ENABLE_IOS='0'
  $env:ANDROID_AGENT_API_TARGET="http://127.0.0.1:$apiPort"
  $env:GITD_CORS_ORIGINS="http://127.0.0.1:$apiPort2,http://localhost:$apiPort2"
  $env:OPENAI_API_KEY='';$env:ANTHROPIC_API_KEY='';$env:OPENROUTER_API_KEY=''
  $api=Start-Process -FilePath $python -ArgumentList @('-m','gitd.cli','up','--host','127.0.0.1','--port',[string]$apiPort) -WorkingDirectory $repo -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path ([string]$ctx.work_root) "android-agent-$Mode-api.out.log") -RedirectStandardError (Join-Path ([string]$ctx.work_root) "android-agent-$Mode-api.err.log")
  $health=Wait-HttpOk "http://127.0.0.1:$apiPort/api/health" $api
  $healthBody=$health.Content|ConvertFrom-Json
  if($healthBody.status -cne 'ok' -or $healthBody.server -cne 'fastapi'){throw 'ANDROID_AGENT_CHECK=FAIL api_health_payload'}
  if($Mode -eq 'functional'){
    $ui=Start-Process -FilePath $node -ArgumentList @($vite,'preview','--host','127.0.0.1','--port',[string]$apiPort2,'--strictPort','--no-open') -WorkingDirectory (Join-Path $repo 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path ([string]$ctx.work_root) 'android-agent-functional-ui.out.log') -RedirectStandardError (Join-Path ([string]$ctx.work_root) 'android-agent-functional-ui.err.log')
    $page=Wait-HttpOk "http://127.0.0.1:$apiPort2/" $ui
    if($page.Content -notmatch '<div id="app"'){throw 'ANDROID_AGENT_FUNCTIONAL=FAIL dashboard_document'}
    $features=Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/api/features" -TimeoutSec 5
    if($null -eq $features.PSObject.Properties['premium_tabs']){throw 'ANDROID_AGENT_FUNCTIONAL=FAIL api_features'}
    $proxiedHealth=Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort2/api/health" -TimeoutSec 5
    if($proxiedHealth.status -cne 'ok'){throw 'ANDROID_AGENT_FUNCTIONAL=FAIL dashboard_api_proxy'}
    Write-Output 'ANDROID_AGENT_FUNCTIONAL=PASS api=health dashboard=served proxy=PASS features=read-only devices_touched=false'
  }else{
    Write-Output 'ANDROID_AGENT_SMOKE=PASS api_health=200 bind=127.0.0.1 devices_touched=false'
  }
}finally{
  foreach($process in @($ui,$api)){
    if($null -ne $process){try{$process.Refresh();if(-not $process.HasExited){$process.Kill();[void]$process.WaitForExit(5000)}}catch{}}
  }
  foreach($key in $saved.Keys){[Environment]::SetEnvironmentVariable($key,[string]$saved[$key],'Process')}
  Remove-Item -LiteralPath $db,$("$db-wal"),$("$db-shm") -Force -ErrorAction SilentlyContinue
}
