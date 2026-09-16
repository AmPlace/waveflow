param(
  [Parameter(Mandatory = $true)]
  [string]$SourceInstaller,
  [Parameter(Mandatory = $true)]
  [string]$Destination,
  [string]$LockFile = "",
  [Parameter(Mandatory = $true)]
  [string]$CaBundleWheel,
[string]$LayoutDirectory = $env:WAVEFLOW_DESKTOP_PYTHON_RUNTIME_LAYOUT
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($LockFile)) {
  $LockFile = Join-Path $Root "desktop_runtime\cpython-3.14.7-windows-x64.json"
}
$SourceInstaller = (Resolve-Path $SourceInstaller).Path
$CaBundleWheel = (Resolve-Path $CaBundleWheel).Path
$LockFile = (Resolve-Path $LockFile).Path

$lock = Get-Content -LiteralPath $LockFile -Raw | ConvertFrom-Json
$source = $lock.source
$ca = $lock.ca_bundle
if ($lock.os -ne "windows" -or $lock.arch -ne "x86_64" -or $lock.python_abi -ne "cp314") {
  throw "Windows x64 CPython lock is incompatible"
}
if ([IO.Path]::GetFileName($SourceInstaller) -ne $source.filename) {
  throw "Python installer filename does not match the provenance lock"
}
$installerHash = (Get-FileHash -LiteralPath $SourceInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
if ($installerHash -ne $source.sha256.ToLowerInvariant()) {
  throw "Python installer SHA-256 does not match the provenance lock"
}

if ([IO.Path]::GetFileName($CaBundleWheel) -ne $ca.filename) {
  throw "CA bundle wheel filename does not match the provenance lock"
}
$caWheelHash = (Get-FileHash -LiteralPath $CaBundleWheel -Algorithm SHA256).Hash.ToLowerInvariant()
if ($caWheelHash -ne $ca.sha256.ToLowerInvariant()) {
  throw "CA bundle wheel SHA-256 does not match the provenance lock"
}

$work = Join-Path ([IO.Path]::GetTempPath()) ("waveflow-python-runtime-" + [guid]::NewGuid().ToString("N"))
$providedLayout = -not [string]::IsNullOrWhiteSpace($LayoutDirectory)
if (-not $providedLayout) {
  throw "Set WAVEFLOW_DESKTOP_PYTHON_RUNTIME_LAYOUT to the locked CPython installer layout"
}
$layoutRoot = (Resolve-Path $LayoutDirectory).Path
$runtimeRoot = Join-Path $work "runtime"
$certifiArchive = Join-Path $work "certifi.zip"
$certifiExtract = Join-Path $work "certifi-extract"
if ($providedLayout) {
  $layoutInstaller = Join-Path $layoutRoot $source.filename
  if (-not (Test-Path -LiteralPath $layoutInstaller -PathType Leaf)) {
    throw "Locked CPython layout is missing $($source.filename)"
  }
  $layoutHash = (Get-FileHash -LiteralPath $layoutInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($layoutHash -ne $source.sha256.ToLowerInvariant()) {
    throw "Locked CPython layout installer hash does not match the provenance lock"
  }
}
$destinationParent = Split-Path -Parent $Destination
$destinationName = Split-Path -Leaf $Destination
$stagingDestination = Join-Path $destinationParent ("." + $destinationName + ".staging-" + [guid]::NewGuid().ToString("N"))
$previousDestination = Join-Path $destinationParent ("." + $destinationName + ".previous-" + [guid]::NewGuid().ToString("N"))
$published = $false

try {
  New-Item -ItemType Directory -Path $runtimeRoot, $certifiExtract, $destinationParent -Force | Out-Null
  Write-Host "Using locked CPython layout: $layoutRoot"

  $runtimeExtractor = Join-Path $Root "scripts\extract-desktop-python-msi.ps1"
  $msiNames = @("core.msi", "exe.msi", "lib.msi", "tcltk.msi", "ucrt.msi")
  foreach ($msiName in $msiNames) {
    $msiPath = Join-Path $layoutRoot $msiName
    if (-not (Test-Path -LiteralPath $msiPath -PathType Leaf)) {
      throw "Python installer layout is missing $msiName"
    }
    $extractArguments = @("-MsiPath", $msiPath, "-RuntimeRoot", $runtimeRoot, "-WorkRoot", $work)
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runtimeExtractor @extractArguments
    if ($LASTEXITCODE -ne 0) {
      throw "MSI runtime extraction failed for $msiName"
    }
  }

  $installedPython = Join-Path $runtimeRoot "python.exe"
  if (-not (Test-Path -LiteralPath $installedPython -PathType Leaf)) {
    throw "MSI extraction did not produce python.exe"
  }
  New-Item -ItemType Directory -Path (Join-Path $runtimeRoot "Scripts") -Force | Out-Null

  Copy-Item -LiteralPath $CaBundleWheel -Destination $certifiArchive -Force
  Expand-Archive -LiteralPath $certifiArchive -DestinationPath $certifiExtract -Force
  $certifiPemSource = Join-Path $certifiExtract "certifi\cacert.pem"
  if (-not (Test-Path -LiteralPath $certifiPemSource -PathType Leaf)) {
    throw "Locked certifi wheel does not contain certifi/cacert.pem"
  }
  $certifiPemHash = (Get-FileHash -LiteralPath $certifiPemSource -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($certifiPemHash -ne $ca.pem_sha256.ToLowerInvariant()) {
    throw "CA bundle PEM SHA-256 does not match the provenance lock"
  }
  $certifiRoot = Join-Path $runtimeRoot "certifi"
  New-Item -ItemType Directory -Path $certifiRoot -Force | Out-Null
  Copy-Item -LiteralPath $certifiPemSource -Destination (Join-Path $certifiRoot "cacert.pem") -Force

  $sitecustomize = @'
"""Use the bundled certifi CA bundle for stdlib TLS."""
from __future__ import annotations

import os
import sys
from pathlib import Path


if not os.environ.get("SSL_CERT_FILE"):
    _candidate = Path(getattr(sys, "base_prefix", sys.prefix)) / "certifi" / "cacert.pem"
    if _candidate.is_file():
        os.environ["SSL_CERT_FILE"] = str(_candidate)
'@
  $sitecustomizePath = Join-Path $runtimeRoot "Lib\sitecustomize.py"
  [IO.File]::WriteAllText($sitecustomizePath, $sitecustomize, [Text.UTF8Encoding]::new($false))

  $metadataWriter = Join-Path $Root "scripts\write-desktop-runtime-metadata.py"
  & $installedPython -B -I $metadataWriter $runtimeRoot $LockFile windows x86_64 python.exe certifi/cacert.pem
  if ($LASTEXITCODE -ne 0) {
    throw "Writing Windows Desktop runtime metadata failed"
  }
  $verifier = Join-Path $Root "scripts\verify-desktop-runtime.py"
  & $installedPython -B -I $verifier $runtimeRoot windows x86_64 3.14
  if ($LASTEXITCODE -ne 0) {
    throw "Verifying Windows Desktop runtime failed"
  }

  if (Test-Path -LiteralPath $stagingDestination) {
    Remove-Item -LiteralPath $stagingDestination -Recurse -Force
  }
  Move-Item -LiteralPath $runtimeRoot -Destination $stagingDestination
  if (Test-Path -LiteralPath $Destination) {
    Move-Item -LiteralPath $Destination -Destination $previousDestination
  }
  Move-Item -LiteralPath $stagingDestination -Destination $Destination
  $published = $true
  if (Test-Path -LiteralPath $previousDestination) {
    Remove-Item -LiteralPath $previousDestination -Recurse -Force
  }
  Write-Host "Built Windows x64 CPython $($lock.python_version) runtime: $Destination"
}
finally {
  if (-not $published -and (Test-Path -LiteralPath $previousDestination) -and -not (Test-Path -LiteralPath $Destination)) {
    Move-Item -LiteralPath $previousDestination -Destination $Destination
  }
  if (Test-Path -LiteralPath $work) {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
  }
  if (-not $published -and (Test-Path -LiteralPath $stagingDestination)) {
    Remove-Item -LiteralPath $stagingDestination -Recurse -Force -ErrorAction SilentlyContinue
  }
}
