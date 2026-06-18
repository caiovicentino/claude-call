# claude-call (Windows) — start a voice call with your Claude Code session.
# Run it from inside the project whose session you want to resume.
# Compativel com Windows PowerShell 5.1 e PowerShell 7+.
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] [string[]]$Args)
$ErrorActionPreference = "Stop"

# Pasta real do script (resolve atalho/symlink se houver)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# Garante o .env (mesmo comportamento do call.sh)
$envFile = Join-Path $ScriptDir ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $ScriptDir ".env.example") $envFile
}

$sub = if ($Args -and $Args.Count -ge 1) { $Args[0] } else { "" }

if ($sub -eq "config") {
    uv run --directory $ScriptDir python configure.py
    exit $LASTEXITCODE
}
if ($sub -eq "doctor") {
    uv run --directory $ScriptDir python doctor.py
    exit $LASTEXITCODE
}

# A sessao a retomar = o diretorio de onde voce chamou (NAO a pasta do repo).
if (-not $env:CALL_CWD) { $env:CALL_CWD = (Get-Location).Path }

Write-Host "claude-call - resuming the Claude Code session in: $env:CALL_CWD"
# build.sh (Swift AEC) e so macOS; no Windows CALL_AEC nao se aplica.
uv run --directory $ScriptDir python call.py
exit $LASTEXITCODE
