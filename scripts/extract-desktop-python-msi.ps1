param(
  [Parameter(Mandatory = $true)]
  [string]$MsiPath,
  [Parameter(Mandatory = $true)]
  [string]$RuntimeRoot,
  [Parameter(Mandatory = $true)]
  [string]$WorkRoot
)

$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace WaveFlowMsi {
  public static class Native {
    public const uint ErrorMoreData = 234;
    public const uint ErrorNoMoreItems = 259;

    [DllImport("msi.dll", CharSet = CharSet.Unicode)]
    public static extern uint MsiOpenDatabase(string path, IntPtr persist, out IntPtr database);
    [DllImport("msi.dll", CharSet = CharSet.Unicode)]
    public static extern uint MsiDatabaseOpenView(IntPtr database, string query, out IntPtr view);
    [DllImport("msi.dll")]
    public static extern uint MsiViewExecute(IntPtr view, IntPtr record);
    [DllImport("msi.dll")]
    public static extern uint MsiViewFetch(IntPtr view, out IntPtr record);
    [DllImport("msi.dll", CharSet = CharSet.Unicode)]
    public static extern uint MsiRecordGetString(IntPtr record, uint field, StringBuilder value, ref uint length);
    [DllImport("msi.dll")]
    public static extern uint MsiRecordDataSize(IntPtr record, uint field);
    [DllImport("msi.dll")]
    public static extern uint MsiRecordReadStream(IntPtr record, uint field, [Out] byte[] data, ref uint length);
    [DllImport("msi.dll")]
    public static extern uint MsiCloseHandle(IntPtr handle);

    public static string ReadString(IntPtr record, uint field) {
      uint length = 256;
      while (true) {
        var value = new StringBuilder((int)length + 1);
        uint capacity = length;
        uint result = MsiRecordGetString(record, field, value, ref capacity);
        if (result == 0) return value.ToString();
        if (result != ErrorMoreData) return string.Empty;
        length = capacity + 1;
      }
    }

    public static byte[] ReadStream(IntPtr record, uint field) {
      uint length = MsiRecordDataSize(record, field);
      if (length == 0) return Array.Empty<byte>();
      if (length > 512 * 1024 * 1024) throw new InvalidDataException("MSI stream too large");
      var data = new byte[(int)length];
      uint actual = length;
      if (MsiRecordReadStream(record, field, data, ref actual) != 0) {
        throw new InvalidDataException("MSI stream read failed");
      }
      if (actual != data.Length) Array.Resize(ref data, (int)actual);
      return data;
    }
  }
}
'@

function Get-MsiRows {
  param(
    [Parameter(Mandatory = $true)] [IntPtr]$Database,
    [Parameter(Mandatory = $true)] [string]$Query,
    [Parameter(Mandatory = $true)] [int]$FieldCount
  )
  $view = [IntPtr]::Zero
  $result = [WaveFlowMsi.Native]::MsiDatabaseOpenView($Database, $Query, [ref]$view)
  if ($result -ne 0) { throw "MSI query failed: $Query (exit code $result)" }
  try {
    $result = [WaveFlowMsi.Native]::MsiViewExecute($view, [IntPtr]::Zero)
    if ($result -ne 0) { throw "MSI query execution failed: $Query (exit code $result)" }
    while ($true) {
      $record = [IntPtr]::Zero
      $result = [WaveFlowMsi.Native]::MsiViewFetch($view, [ref]$record)
      if ($result -eq [WaveFlowMsi.Native]::ErrorNoMoreItems) { break }
      if ($result -ne 0) { throw "MSI row fetch failed: $Query (exit code $result)" }
      try {
        $row = [ordered]@{}
        for ($field = 1; $field -le $FieldCount; $field++) {
          $row["f$field"] = [WaveFlowMsi.Native]::ReadString($record, [uint32]$field)
        }
        [pscustomobject]$row
      } finally {
        if ($record -ne [IntPtr]::Zero) { [void][WaveFlowMsi.Native]::MsiCloseHandle($record) }
      }
    }
  } finally {
    if ($view -ne [IntPtr]::Zero) { [void][WaveFlowMsi.Native]::MsiCloseHandle($view) }
  }
}

function Get-MsiCabinet {
  param(
    [Parameter(Mandatory = $true)] [string]$MsiPath,
    [Parameter(Mandatory = $true)] [string]$CabinetPath
  )
  $database = [IntPtr]::Zero
  $result = [WaveFlowMsi.Native]::MsiOpenDatabase($MsiPath, [IntPtr]::Zero, [ref]$database)
  if ($result -ne 0) { throw "MSI database could not be opened: $MsiPath (exit code $result)" }
  try {
    $view = [IntPtr]::Zero
    $result = [WaveFlowMsi.Native]::MsiDatabaseOpenView($database, "SELECT * FROM _Streams", [ref]$view)
    if ($result -ne 0) { throw "MSI streams table could not be opened: $MsiPath (exit code $result)" }
    try {
      $result = [WaveFlowMsi.Native]::MsiViewExecute($view, [IntPtr]::Zero)
      if ($result -ne 0) { throw "MSI streams query failed: $MsiPath (exit code $result)" }
      $found = $false
      while (-not $found) {
        $record = [IntPtr]::Zero
        $result = [WaveFlowMsi.Native]::MsiViewFetch($view, [ref]$record)
        if ($result -eq [WaveFlowMsi.Native]::ErrorNoMoreItems) { break }
        if ($result -ne 0) { throw "MSI stream row fetch failed: $MsiPath (exit code $result)" }
        try {
          $name = [WaveFlowMsi.Native]::ReadString($record, 1)
          if ($name -match '\.cab$' -and [WaveFlowMsi.Native]::MsiRecordDataSize($record, 2) -gt 0) {
            [IO.File]::WriteAllBytes($CabinetPath, [WaveFlowMsi.Native]::ReadStream($record, 2))
            $found = $true
          }
        } finally {
          if ($record -ne [IntPtr]::Zero) { [void][WaveFlowMsi.Native]::MsiCloseHandle($record) }
        }
      }
      if (-not $found) { throw "MSI does not contain an embedded cabinet: $MsiPath" }
    } finally {
      if ($view -ne [IntPtr]::Zero) { [void][WaveFlowMsi.Native]::MsiCloseHandle($view) }
    }
  } finally {
    if ($database -ne [IntPtr]::Zero) { [void][WaveFlowMsi.Native]::MsiCloseHandle($database) }
  }
}

function Resolve-MsiDirectoryPath {
  param(
    [Parameter(Mandatory = $true)] [string]$DirectoryId,
    [Parameter(Mandatory = $true)] [hashtable]$Directories,
    [Parameter(Mandatory = $true)] [hashtable]$Cache
  )
  if ($Cache.ContainsKey($DirectoryId)) { return $Cache[$DirectoryId] }
  if ($DirectoryId -eq "TARGETDIR") { $Cache[$DirectoryId] = ""; return "" }
  if (-not $Directories.ContainsKey($DirectoryId)) { throw "MSI directory is missing: $DirectoryId" }
  $entry = $Directories[$DirectoryId]
  $parent = [string]$entry.f2
  $default = [string]$entry.f3
  $separator = $default.IndexOf('|')
  $name = if ($separator -ge 0) { $default.Substring($separator + 1) } else { $default }
  $parentPath = if ([string]::IsNullOrWhiteSpace($parent)) { "" } else {
    Resolve-MsiDirectoryPath $parent $Directories $Cache
  }
  if ([string]::IsNullOrWhiteSpace($name) -or $name -eq ".") {
    $resolved = $parentPath
  } elseif ([string]::IsNullOrWhiteSpace($parentPath)) {
    $resolved = $name
  } else {
    $resolved = Join-Path $parentPath $name
  }
  $Cache[$DirectoryId] = $resolved
  return $resolved
}

function Expand-MsiIntoRuntime {
  param(
    [Parameter(Mandatory = $true)] [string]$MsiPath,
    [Parameter(Mandatory = $true)] [string]$RuntimeRoot,
    [Parameter(Mandatory = $true)] [string]$WorkRoot
  )
  $name = [IO.Path]::GetFileNameWithoutExtension($MsiPath)
  $cabinetPath = Join-Path $WorkRoot "$name.cab"
  $expandedRoot = Join-Path $WorkRoot "expanded-$name"
  New-Item -ItemType Directory -Path $expandedRoot -Force | Out-Null
  Get-MsiCabinet $MsiPath $cabinetPath
  $expandArguments = @("-F:*", ('"' + $cabinetPath + '"'), ('"' + $expandedRoot + '"'))
  $expander = Join-Path $env:WINDIR "System32\expand.exe"
  $expanded = Start-Process -FilePath $expander -ArgumentList $expandArguments -Wait -PassThru -WindowStyle Hidden
  if ($expanded.ExitCode -ne 0) { throw "CAB extraction failed for $MsiPath with exit code $($expanded.ExitCode)" }

  $database = [IntPtr]::Zero
  $result = [WaveFlowMsi.Native]::MsiOpenDatabase($MsiPath, [IntPtr]::Zero, [ref]$database)
  if ($result -ne 0) { throw "MSI database could not be reopened: $MsiPath (exit code $result)" }
  try {
    $directoryRows = @(Get-MsiRows $database "SELECT * FROM Directory" 3)
    $directories = @{}
    foreach ($row in $directoryRows) { $directories[[string]$row.f1] = $row }
    $directoryCache = @{}
    $componentRows = @(Get-MsiRows $database "SELECT * FROM Component" 3)
    $componentDirectories = @{}
    foreach ($row in $componentRows) { $componentDirectories[[string]$row.f1] = [string]$row.f3 }
    $fileRows = @(Get-MsiRows $database "SELECT * FROM File" 4)
    foreach ($row in $fileRows) {
      $sourceName = [string]$row.f1
      $component = [string]$row.f2
      $fileName = [string]$row.f3
      $separator = $fileName.IndexOf('|')
      $targetName = if ($separator -ge 0) { $fileName.Substring($separator + 1) } else { $fileName }
      if (-not $componentDirectories.ContainsKey($component)) { throw "MSI component directory is missing: $component" }
      $relativeDirectory = Resolve-MsiDirectoryPath $componentDirectories[$component] $directories $directoryCache
      $sourcePath = Join-Path $expandedRoot $sourceName
      if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) { throw "CAB file is missing for MSI entry: $sourceName" }
      $expectedSize = 0L
      if (-not [long]::TryParse([string]$row.f4, [Globalization.NumberStyles]::Integer,
                                 [Globalization.CultureInfo]::InvariantCulture, [ref]$expectedSize)) {
        throw "MSI file size is invalid: $sourceName"
      }
      if ((Get-Item -LiteralPath $sourcePath).Length -ne $expectedSize) { throw "CAB file size does not match MSI metadata: $sourceName" }
      $targetDirectory = if ([string]::IsNullOrWhiteSpace($relativeDirectory)) { $RuntimeRoot } else {
        Join-Path $RuntimeRoot $relativeDirectory
      }
      New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
      Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $targetDirectory $targetName) -Force
    }
  } finally {
    if ($database -ne [IntPtr]::Zero) { [void][WaveFlowMsi.Native]::MsiCloseHandle($database) }
  }
}

if (-not (Test-Path -LiteralPath $MsiPath -PathType Leaf)) { throw "MSI input is missing: $MsiPath" }
New-Item -ItemType Directory -Path $RuntimeRoot, $WorkRoot -Force | Out-Null
Expand-MsiIntoRuntime (Resolve-Path $MsiPath).Path (Resolve-Path $RuntimeRoot).Path (Resolve-Path $WorkRoot).Path
