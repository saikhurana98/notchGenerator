<#
.SYNOPSIS
  Set up and start notchgen on Windows, without Docker.

.DESCRIPTION
  Clones the repo (if not already run from inside it), installs uv if needed, syncs
  the virtualenv, registers the `notchgen` and `notchgen-web` commands globally via
  `uv tool install`, then starts the portal and opens it in your browser.

.EXAMPLE
  irm https://raw.githubusercontent.com/SanchakGarg/notchGenerator/main/install.ps1 | iex

.EXAMPLE
  ./install.ps1 -Port 9000 -NoBrowser
#>

param(
    [int]$Port = 8000,
    [switch]$NoBrowser,
    [string]$InstallDir = (Join-Path $env:USERPROFILE "notchgen")
)

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/SanchakGarg/notchGenerator.git"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    * $msg" -ForegroundColor Green }
function Info($msg) { Write-Host "    $msg" }
function Warn($msg) { Write-Host "    ! $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }

if ($Port -le 0 -or $Port -ge 65536) { Die "not a valid port: $Port" }

# ---------------------------------------------------------------- locate or fetch the repo

$repoDir = $null
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "pyproject.toml"))) {
    $repoDir = $PSScriptRoot
} elseif (Test-Path (Join-Path $InstallDir "pyproject.toml")) {
    $repoDir = $InstallDir
}

if (-not $repoDir) {
    Step "Fetching notchgen"
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Die "git is required to fetch notchgen. Install it from https://git-scm.com/download/win and re-run."
    }
    git clone --depth 1 $RepoUrl $InstallDir
    $repoDir = $InstallDir
    Ok "cloned to $repoDir"
} else {
    Ok "using existing checkout at $repoDir"
}

Set-Location $repoDir

# ---------------------------------------------------------------- uv

Step "Checking for uv"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Info "installing uv"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Die "uv was installed but is not on PATH yet. Open a new terminal and re-run this script."
}
Ok (uv --version)

Step "Installing dependencies"
uv sync --frozen
Ok "virtualenv ready at .venv"

Step "Registering the notchgen and notchgen-web commands"
uv tool install --force --editable . | Out-Null
try {
    uv tool update-shell | Out-Null
} catch {
    Warn "could not update PATH automatically; open a new terminal before using 'notchgen-web' directly"
}
Ok "installed — 'notchgen' and 'notchgen-web' are now on your PATH (new terminals)"

# ---------------------------------------------------------------- run

Step "Starting the portal on port $Port"
Info "press Ctrl-C to stop"
if ($NoBrowser) {
    uv run notchgen-web --port $Port --no-browser
} else {
    uv run notchgen-web --port $Port
}
