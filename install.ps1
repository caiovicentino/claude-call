# claude-call — one-shot installer for Windows (native, no WSL).
# Run it from a clone:  .\install.ps1
#
# Installs (via scoop): uv, ffmpeg, whisper.cpp; then python deps (uv sync),
# a whisper model, and a global `claude-call` command. (Claude Code itself you
# install + log in once — it's the brain.)
[CmdletBinding()]
param([string]$Model = "small")
$ErrorActionPreference = "Stop"

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn ($m) { Write-Host "!!  $m" -ForegroundColor Yellow }
function Have ($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }

$RepoDir = $PSScriptRoot
Say "claude-call installer (Windows)"

# --- 1) scoop (user-level package manager, no admin) ---
if (-not (Have scoop)) {
    Warn "scoop nao encontrado. Instale com:"
    Warn '  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned; irm get.scoop.sh | iex'
    throw "scoop e necessario (ou instale uv/ffmpeg/whisper-cpp manualmente e rode 'uv sync')."
}

# --- 2) system deps via scoop ---
Say "Installing uv, ffmpeg, whisper-cpp (scoop)..."
scoop install uv ffmpeg whisper-cpp

# --- 3) python deps ---
Say "Installing Python deps (uv sync)..."
uv sync --directory $RepoDir

# --- 4) whisper model ---
$modelPath = Join-Path $HOME ".cache\whisper\ggml-$Model.bin"
if (-not (Test-Path $modelPath)) {
    Say "Downloading whisper model ($Model)..."
    & (Join-Path $RepoDir "scripts\download-model.ps1") $Model
}

# --- 5) global command: a .cmd shim on PATH (scoop shims dir) ---
$shimDir = Join-Path $HOME "scoop\shims"
if (-not (Test-Path $shimDir)) { $shimDir = $RepoDir; Warn "scoop shims dir nao achado; usando $RepoDir" }
$shim = Join-Path $shimDir "claude-call.cmd"
$callPs1 = Join-Path $RepoDir "call.ps1"
@"
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "$callPs1" %*
"@ | Set-Content -Path $shim -Encoding ascii
Say "Installed command: $shim"

# --- 6) Claude Code (the brain) ---
if (-not (Have claude)) {
    Warn "Claude Code isn't installed (it's the brain). Get it at https://docs.claude.com/claude-code"
    Warn "Then run 'claude' once and log in. claude-call uses your subscription — no API key."
}

# --- 7) verify + benchmark ---
if ($env:CLAUDE_CALL_NO_DOCTOR -ne "1") {
    Write-Host ""
    Say "Verifying your setup..."
    try { uv run --directory $RepoDir python doctor.py } catch { }
}

Write-Host ""
Say "Done! Start a call from any project you've used with Claude Code:"
Write-Host "      cd C:\your-project ; claude-call"
