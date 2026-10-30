#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][string]$ContextPath)
$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
& (Join-Path $PSScriptRoot 'web-local-check.ps1') -ContextPath $ContextPath -Mode functional
