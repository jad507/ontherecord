# Installs On the Record into the folder this script sits in: uv (which brings its own Python), the packages from
# uv.lock (with the PyTorch build that suits the graphics card), ffmpeg and llama.cpp (pinned and checked), the models
# this computer can run, and a desktop shortcut.
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Write-Host "Installing On the Record in $Repo`n"
# uv 0.11.25 from its GitHub release, kept only if its SHA-256 is the one written here (GitHub's digest for the asset).
# uv's own install.ps1 does not check what it downloads, so this does it instead.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $UvVersion = "0.11.25"
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") {
        $UvAsset = "uv-aarch64-pc-windows-msvc.zip"; $UvSha256 = "40d65c29c4d97db6a0993df665d3727700bb799b3618992ce9a4dc533c6d1a31"
    } else {
        $UvAsset = "uv-x86_64-pc-windows-msvc.zip"; $UvSha256 = "15bfd1423b7eaa7aae949922d4712ebaac2bb44a81af64ab59bbe007090cb0d0"
    }
    Write-Host "Installing uv $UvVersion, the Python installer (https://docs.astral.sh/uv/)..."
    $UvTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("uv-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $UvTmp | Out-Null
    $UvZip = Join-Path $UvTmp $UvAsset
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/astral-sh/uv/releases/download/$UvVersion/$UvAsset" -OutFile $UvZip
    if ((Get-FileHash -Algorithm SHA256 $UvZip).Hash.ToLower() -ne $UvSha256) {
        Remove-Item -Recurse -Force $UvTmp
        throw "uv's download does not match its pinned SHA-256, so it was not installed. Stopping."
    }
    Expand-Archive -Path $UvZip -DestinationPath $UvTmp
    $UvBin = Join-Path $env:USERPROFILE ".local\bin"
    New-Item -ItemType Directory -Force -Path $UvBin | Out-Null
    Copy-Item (Join-Path $UvTmp "uv.exe"), (Join-Path $UvTmp "uvx.exe"), (Join-Path $UvTmp "uvw.exe") $UvBin
    Remove-Item -Recurse -Force $UvTmp
    $env:Path = "$UvBin;$env:Path"
}
Set-Location $Repo
Write-Host "`nLooking at the graphics card, to pick the right PyTorch build..."
$Flavor = "$(uv run --no-project --python 3.12 "$Repo\src\ontherecord\hwdetect.py" --torch-flavor)".Trim()
if ($LASTEXITCODE -ne 0 -or -not $Flavor) { $Flavor = "cu128" }
Write-Host "`nInstalling Python and the packages (a few GB the first time)..."
uv sync --extra all --frozen --no-default-groups --group $Flavor
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
& "$Repo\.venv\Scripts\ontherecord.exe" setup --install
