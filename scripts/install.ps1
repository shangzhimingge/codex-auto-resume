[CmdletBinding()]
param(
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }),
    [switch]$DisableDefaultActivation
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$SourceSkill = Join-Path $RepoRoot "skill\codex-auto-resume"
$BlockPath = Join-Path $RepoRoot "activation\AGENTS.block.md"
$CodexHome = [System.IO.Path]::GetFullPath($CodexHome)
$SkillsRoot = Join-Path $CodexHome "skills"
$Destination = Join-Path $SkillsRoot "codex-auto-resume"
$AgentsPath = Join-Path $CodexHome "AGENTS.md"

if (-not (Test-Path -LiteralPath (Join-Path $SourceSkill "SKILL.md"))) {
    throw "Skill source is incomplete: $SourceSkill"
}
New-Item -ItemType Directory -Force -Path $SkillsRoot | Out-Null

$Stage = Join-Path $SkillsRoot ".codex-auto-resume.install-$PID"
if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
Copy-Item -LiteralPath $SourceSkill -Destination $Stage -Recurse
if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
Move-Item -LiteralPath $Stage -Destination $Destination

$Existing = if (Test-Path -LiteralPath $AgentsPath) { Get-Content -LiteralPath $AgentsPath -Raw -Encoding UTF8 } else { "" }
$Begin = "<!-- BEGIN CODEX-AUTO-RESUME MANAGED BLOCK -->"
$End = "<!-- END CODEX-AUTO-RESUME MANAGED BLOCK -->"
$Pattern = "(?ms)^" + [regex]::Escape($Begin) + ".*?^" + [regex]::Escape($End) + "\r?\n?"
$Base = [regex]::Replace($Existing, $Pattern, "").TrimEnd()

if ($DisableDefaultActivation) {
    $Updated = if ($Base) { $Base + "`n" } else { "" }
} else {
    $Block = (Get-Content -LiteralPath $BlockPath -Raw -Encoding UTF8).Trim()
    $Updated = if ($Base) { $Base + "`n`n" + $Block + "`n" } else { $Block + "`n" }
}

New-Item -ItemType Directory -Force -Path $CodexHome | Out-Null
$TemporaryAgents = Join-Path $CodexHome ".AGENTS.md.install-$PID.tmp"
[System.IO.File]::WriteAllText($TemporaryAgents, $Updated, [System.Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $TemporaryAgents -Destination $AgentsPath -Force

[pscustomobject]@{
    skill = $Destination
    agents = $AgentsPath
    default_activation = -not $DisableDefaultActivation
    version = (Get-Content -LiteralPath (Join-Path $Destination "VERSION") -Raw).Trim()
} | ConvertTo-Json -Compress
