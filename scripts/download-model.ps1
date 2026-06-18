# Downloads a whisper.cpp ggml model into %USERPROFILE%\.cache\whisper\
# Usage: .\scripts\download-model.ps1 [tiny|base|small|medium|large-v3-turbo]   (default: small)
[CmdletBinding()]
param([string]$Model = "small")
$ErrorActionPreference = "Stop"

$dest = Join-Path $HOME ".cache\whisper"
$file = "ggml-$Model.bin"
$url  = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$file"
$out  = Join-Path $dest $file

New-Item -ItemType Directory -Force -Path $dest | Out-Null
if (Test-Path $out) {
    Write-Host "already have $out"
    exit 0
}
Write-Host "downloading $file ..."
# curl.exe (presente no Windows 10+) e bem mais rapido que Invoke-WebRequest p/ arquivos grandes
curl.exe -fL --retry 3 -o $out $url
Write-Host "saved to $out"
Write-Host "set CALL_WHISPER_MODEL=$out  (or it's the default for 'small')"
