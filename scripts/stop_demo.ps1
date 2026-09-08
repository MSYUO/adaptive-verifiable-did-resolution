[CmdletBinding()]
param(
    [string]$StatePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $StatePath) {
    $StatePath = Join-Path $RepoRoot ".avdr-demo\session.json"
}
if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
    Write-Output "AVDR_DEMO_STATUS=NOT_RUNNING"
    Write-Output "RESIDUAL_SERVICE_LISTENERS=0"
    exit 0
}

$session = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
if ($session.schema_version -ne "avdr-demo-session-v1") {
    throw "Unrecognized demo session schema in $StatePath"
}
if ([System.IO.Path]::GetFullPath($session.repo_root) -ne $RepoRoot) {
    throw "Session state belongs to another repository: $($session.repo_root)"
}

$unsafe = @()
$entries = @($session.processes)
[array]::Reverse($entries)
foreach ($entry in $entries) {
    $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        continue
    }

    $cim = Get-CimInstance Win32_Process `
        -Filter ("ProcessId=" + $entry.pid) `
        -ErrorAction SilentlyContinue
    $commandLine = if ($null -ne $cim) { [string]$cim.CommandLine } else { "" }
    $actualPath = try { $process.Path } catch { "" }
    $expectedStart = [DateTime]::Parse($entry.start_time_utc).ToUniversalTime()
    $actualStart = $process.StartTime.ToUniversalTime()
    $startDelta = [Math]::Abs(($actualStart - $expectedStart).TotalSeconds)
    $identityMatches = (
        $actualPath -eq $entry.executable -and
        $commandLine.Contains([string]$entry.module) -and
        $commandLine.Contains("--port $($entry.port)") -and
        $startDelta -lt 2
    )
    if (-not $identityMatches) {
        $unsafe += [pscustomobject]@{
            role = $entry.role
            pid = $entry.pid
            reason = "PID identity did not match recorded executable/module/port/start time"
        }
        continue
    }

    Stop-Process -Id $entry.pid -Force
    try {
        Wait-Process -Id $entry.pid -Timeout 10 -ErrorAction SilentlyContinue
    }
    catch {
        # The listener check below is the final shutdown authority.
    }
}

if ($unsafe.Count -gt 0) {
    $detail = $unsafe | ConvertTo-Json -Compress
    throw "Refused to stop process entries whose identity changed. State retained: $detail"
}

Start-Sleep -Milliseconds 400
$ports = @(
    $session.ports.backend,
    $session.ports.resolver_a,
    $session.ports.resolver_b,
    $session.ports.resolver_c
)
$listeners = @(
    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in $ports }
)
Write-Output "RESIDUAL_SERVICE_LISTENERS=$($listeners.Count)"
if ($listeners.Count -gt 0) {
    throw "One or more demo ports remain occupied; session state retained for inspection."
}

Remove-Item -LiteralPath $StatePath -Force
$stateDirectory = Split-Path -Parent $StatePath
if ((Test-Path -LiteralPath $stateDirectory) -and
    -not (Get-ChildItem -LiteralPath $stateDirectory -Force)) {
    Remove-Item -LiteralPath $stateDirectory -Force
}
Write-Output "AVDR_DEMO_STATUS=STOPPED"
