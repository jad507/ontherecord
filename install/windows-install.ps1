# Installs On the Record into the folder this script sits in: uv (which brings its own Python), the packages from
# uv.lock (with the PyTorch build that suits the graphics card), ffmpeg and llama.cpp (pinned and checked), the models
# this computer can run, and a desktop shortcut.
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Write-Host "Installing On the Record in $Repo`n"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv, the Python installer (https://docs.astral.sh/uv/)..."
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Set-Location $Repo
Write-Host "`nLooking at the graphics card, to pick the right PyTorch build..."
$Flavor = "$(uv run --no-project --python 3.12 "$Repo\src\ontherecord\hwdetect.py" --torch-flavor)".Trim()
if ($LASTEXITCODE -ne 0 -or -not $Flavor) { $Flavor = "cu128" }
Write-Host "`nInstalling Python and the packages (a few GB the first time)..."
uv sync --extra all --frozen --no-default-groups --group $Flavor
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
& "$Repo\.venv\Scripts\ontherecord.exe" setup --install
