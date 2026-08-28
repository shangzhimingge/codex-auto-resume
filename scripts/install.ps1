[CmdletBinding()]
param(
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }),
    [switch]$DisableDefaultActivation,
    [switch]$AdoptExisting
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Arguments = @((Join-Path $RepoRoot "installer.py"), "install", "--codex-home", $CodexHome)
if ($DisableDefaultActivation) { $Arguments += "--disable-default-activation" }
if ($AdoptExisting) { $Arguments += "--adopt-existing" }
& python @Arguments
exit $LASTEXITCODE
