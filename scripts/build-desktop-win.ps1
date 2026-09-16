param(
  [string]$PythonInstaller = $env:WAVEFLOW_DESKTOP_PYTHON_RUNTIME_INSTALLER,
  [string]$PythonRuntimeLock = $env:WAVEFLOW_DESKTOP_PYTHON_RUNTIME_LOCK,
  [string]$PythonRuntimeLayout = $env:WAVEFLOW_DESKTOP_PYTHON_RUNTIME_LAYOUT,
  [string]$CaBundleWheel = $env:WAVEFLOW_DESKTOP_CA_BUNDLE_WHEEL,
  [string]$FfmpegPath = $env:WAVEFLOW_DESKTOP_FFMPEG,
  [string]$FfmpegSha256 = $env:WAVEFLOW_DESKTOP_FFMPEG_SHA256
)

$ErrorActionPreference = "Stop"
$InitialLocation = (Get-Location).Path
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($PythonRuntimeLock)) {
  $PythonRuntimeLock = Join-Path $Root "desktop_runtime\cpython-3.14.7-windows-x64.json"
}
if ([string]::IsNullOrWhiteSpace($FfmpegPath)) {
  $FfmpegPath = Join-Path $Root "ffmpeg\win-x64\ffmpeg.exe"
}

$backendDist = Join-Path $Root "backend_dist"
$backendStage = Join-Path $Root (".backend_dist.staging-" + [guid]::NewGuid().ToString("N"))
$backendPrevious = Join-Path $Root (".backend_dist.previous-" + [guid]::NewGuid().ToString("N"))
$buildVenv = Join-Path $Root (".codex-tmp\desktop-build-venv-" + [guid]::NewGuid().ToString("N"))
$published = $false
$buildVenvCreated = $false

try {
  Push-Location $Root

  Write-Host "== Build frontend =="
  Push-Location (Join-Path $Root "frontend")
  npm install
  if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed" }
  npm run build -- --mode desktop
  if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
  Pop-Location

  Write-Host "== Stage locked Python runtime =="
  New-Item -ItemType Directory -Path $backendStage -Force | Out-Null
  if ([string]::IsNullOrWhiteSpace($PythonInstaller)) {
    throw "Set WAVEFLOW_DESKTOP_PYTHON_RUNTIME_INSTALLER to the locked Python 3.14.7 Windows installer"
  }
  if ([string]::IsNullOrWhiteSpace($CaBundleWheel)) {
    throw "Set WAVEFLOW_DESKTOP_CA_BUNDLE_WHEEL to the locked certifi wheel"
  }
  $runtimeBuilder = Join-Path $Root "scripts\build-desktop-python-runtime.ps1"
  $runtimeArguments = @(
    "-SourceInstaller", $PythonInstaller,
    "-Destination", (Join-Path $backendStage "python-runtime"),
    "-LockFile", $PythonRuntimeLock,
    "-CaBundleWheel", $CaBundleWheel
  )
  if (-not [string]::IsNullOrWhiteSpace($PythonRuntimeLayout)) {
    $runtimeArguments += @("-LayoutDirectory", $PythonRuntimeLayout)
  }
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runtimeBuilder @runtimeArguments
  if ($LASTEXITCODE -ne 0) {
    throw "Windows Python runtime staging failed"
  }

  Write-Host "== Build backend with staged Python =="
  $runtimePython = Join-Path $backendStage "python-runtime\python.exe"
  & $runtimePython -B -I -m venv $buildVenv
  if ($LASTEXITCODE -ne 0) { throw "Backend build virtual environment creation failed" }
  $buildVenvCreated = $true
  Push-Location (Join-Path $Root "backend")
  & (Join-Path $buildVenv "Scripts\python.exe") -m pip install -r requirements.txt
  if ($LASTEXITCODE -ne 0) { throw "Backend dependency installation failed" }
  & (Join-Path $buildVenv "Scripts\python.exe") -m pip install pyinstaller
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed" }
  & (Join-Path $buildVenv "Scripts\pyinstaller.exe") `
    --clean `
    --noconfirm `
    --onedir `
    --hidden-import adapters.17live `
    --collect-data zhconv `
    --add-data "config;config" `
    --add-data "official_plugins;official_plugins" `
    --add-data "bundled_plugins;bundled_plugins" `
    --name waveflow-backend `
    desktop_entry.py
  Pop-Location

  Write-Host "== Stage backend bundle =="
  New-Item -ItemType Directory -Path $backendStage -Force | Out-Null
  Copy-Item -Path (Join-Path $Root "backend\dist\waveflow-backend\*") `
    -Destination $backendStage -Recurse -Force


  Write-Host "== Bundle ffmpeg =="
  if (-not (Test-Path -LiteralPath $FfmpegPath -PathType Leaf)) {
    throw "An explicit Windows x64 ffmpeg.exe artifact is required: $FfmpegPath"
  }
  if ([string]::IsNullOrWhiteSpace($FfmpegSha256)) {
    throw "Set WAVEFLOW_DESKTOP_FFMPEG_SHA256 for the explicit ffmpeg.exe artifact"
  }
  $actualFfmpegSha256 = (Get-FileHash -LiteralPath $FfmpegPath -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actualFfmpegSha256 -ne $FfmpegSha256.Trim().ToLowerInvariant()) {
    throw "ffmpeg.exe SHA-256 does not match WAVEFLOW_DESKTOP_FFMPEG_SHA256"
  }
  Copy-Item -LiteralPath $FfmpegPath -Destination (Join-Path $backendStage "ffmpeg.exe") -Force

  Write-Host "== Publish complete backend bundle =="
  if (Test-Path -LiteralPath $backendPrevious) {
    Remove-Item -LiteralPath $backendPrevious -Recurse -Force
  }
  if (Test-Path -LiteralPath $backendDist) {
    Move-Item -LiteralPath $backendDist -Destination $backendPrevious
  }
  Move-Item -LiteralPath $backendStage -Destination $backendDist
  $published = $true
  if (Test-Path -LiteralPath $backendPrevious) {
    Remove-Item -LiteralPath $backendPrevious -Recurse -Force
  }

  Write-Host "== Install desktop deps =="
  npm install
  if ($LASTEXITCODE -ne 0) { throw "Desktop dependency installation failed" }
  Write-Host "== Build Electron main/preload =="
  npm run build:electron
  if ($LASTEXITCODE -ne 0) { throw "Electron main/preload build failed" }
  Write-Host "== Build Windows directory, zip, and NSIS artifacts =="
  npx electron-builder --win --x64
  if ($LASTEXITCODE -ne 0) { throw "Windows Electron packaging failed" }
  Write-Host "== Done =="
}
finally {
  Set-Location $InitialLocation
  if (-not $published -and (Test-Path -LiteralPath $backendPrevious) -and -not (Test-Path -LiteralPath $backendDist)) {
    Move-Item -LiteralPath $backendPrevious -Destination $backendDist
  }
  if (Test-Path -LiteralPath $backendStage) {
    Remove-Item -LiteralPath $backendStage -Recurse -Force -ErrorAction SilentlyContinue
  }
  if (Test-Path -LiteralPath $backendPrevious) {
    Remove-Item -LiteralPath $backendPrevious -Recurse -Force -ErrorAction SilentlyContinue
  }
  if ($buildVenvCreated -and (Test-Path -LiteralPath $buildVenv)) {
    Remove-Item -LiteralPath $buildVenv -Recurse -Force -ErrorAction SilentlyContinue
  }
}
