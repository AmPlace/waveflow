param(
  [Parameter(Mandatory = $true)]
  [string]$SourceInstaller,
  [Parameter(Mandatory = $true)]
  [string]$CaBundleWheel,
  [string]$LockFile = "",
  [string]$LayoutDirectory = $env:WAVEFLOW_DESKTOP_PYTHON_RUNTIME_LAYOUT
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Builder = Join-Path $Root "scripts\build-desktop-python-runtime.ps1"
$Verifier = Join-Path $Root "scripts\verify-desktop-runtime.py"
if ([string]::IsNullOrWhiteSpace($LockFile)) {
  $LockFile = Join-Path $Root "desktop_runtime\cpython-3.14.7-windows-x64.json"
}
$SourceInstaller = (Resolve-Path $SourceInstaller).Path
$CaBundleWheel = (Resolve-Path $CaBundleWheel).Path
$LockFile = (Resolve-Path $LockFile).Path
if ([string]::IsNullOrWhiteSpace($LayoutDirectory)) {
  $candidateLayout = Join-Path (Split-Path -Parent $SourceInstaller) "python-installer-layout"
  if (Test-Path -LiteralPath $candidateLayout -PathType Container) {
    $LayoutDirectory = $candidateLayout
  }
}
if (-not [string]::IsNullOrWhiteSpace($LayoutDirectory)) {
  $LayoutDirectory = (Resolve-Path $LayoutDirectory).Path
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("waveflow-runtime-repeat-" + [guid]::NewGuid().ToString("N"))
$first = Join-Path $testRoot "stage one with spaces"
$second = Join-Path $testRoot "stage two with spaces"
$passed = $false

function Invoke-Stage {
  param([string]$Destination)
  $runtimeDestination = Join-Path $Destination "python-runtime"
  $arguments = @(
    "-SourceInstaller", $SourceInstaller,
    "-Destination", $runtimeDestination,
    "-LockFile", $LockFile,
    "-CaBundleWheel", $CaBundleWheel
  )
  if (-not [string]::IsNullOrWhiteSpace($LayoutDirectory)) {
    $arguments += @("-LayoutDirectory", $LayoutDirectory)
  }
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Builder @arguments
  if ($LASTEXITCODE -ne 0) { throw "runtime staging failed for $Destination" }
}

function Assert-Runtime {
  param([string]$RootPath)
  $runtime = Join-Path $RootPath "python-runtime"
  $python = Join-Path $runtime "python.exe"
  foreach ($required in @(
    $python,
    (Join-Path $runtime "python3.dll"),
    (Join-Path $runtime "python314.dll"),
    (Join-Path $runtime "runtime.json")
  )) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
      throw "runtime validation file is missing: $required"
    }
  }
  foreach ($requiredDirectory in @("Lib", "DLLs", "Scripts")) {
    if (-not (Test-Path -LiteralPath (Join-Path $runtime $requiredDirectory) -PathType Container)) {
      throw "runtime validation directory is missing: $requiredDirectory"
    }
  }
  $probe = (& $python -B -I -c "import json, platform, struct, sys; print(json.dumps({'version': '.'.join(map(str, sys.version_info[:3])), 'machine': platform.machine(), 'bits': struct.calcsize('P') * 8}))").Trim()
  if ($LASTEXITCODE -ne 0) { throw "staged python probe failed: $python" }
  $identity = $probe | ConvertFrom-Json
  if ($identity.version -ne "3.14.7" -or $identity.bits -ne 64 -or
      $identity.machine.ToLowerInvariant() -notin @("amd64", "x86_64")) {
    throw "staged Python identity is invalid: $probe"
  }
  & $python -B -I $Verifier $runtime windows x86_64 3.14
  if ($LASTEXITCODE -ne 0) { throw "runtime integrity verification failed: $runtime" }
  return [pscustomobject]@{
    Runtime = $runtime
    Metadata = Get-Content (Join-Path $runtime "runtime.json") -Raw | ConvertFrom-Json
  }
}

try {
  Invoke-Stage $first
  $stageOne = Assert-Runtime $first
  Invoke-Stage $second
  $stageTwo = Assert-Runtime $second
  if ($stageOne.Metadata.tree_sha256 -ne $stageTwo.Metadata.tree_sha256 -or
      $stageOne.Metadata.tree_file_count -ne $stageTwo.Metadata.tree_file_count) {
    throw "runtime metadata differs between repeated staging runs"
  }
  $fingerprints = @()
  foreach ($stage in @($stageOne, $stageTwo)) {
    $entries = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $stage.Runtime -Recurse -File -Force | Sort-Object FullName)) {
      $relative = $file.FullName.Substring($stage.Runtime.Length).TrimStart('\').Replace('\', '/')
      $entries += "$relative|$($file.Length)|$((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant())"
    }
    $fingerprints += ,$entries
  }
  if ($null -ne (Compare-Object -ReferenceObject $fingerprints[0] -DifferenceObject $fingerprints[1])) {
    throw "runtime file trees differ between repeated staging runs"
  }
  Write-Host "Windows CPython staging repeatability: PASS"
  Write-Host "runtime tree SHA-256: $($stageOne.Metadata.tree_sha256)"
  Write-Host "runtime file count: $($stageOne.Metadata.tree_file_count)"
  $passed = $true
}
finally {
  if (Test-Path -LiteralPath $testRoot) {
    if ($passed) {
      Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    } else {
      Write-Host "preserved failed repeatability workspace: $testRoot"
    }
  }
}
