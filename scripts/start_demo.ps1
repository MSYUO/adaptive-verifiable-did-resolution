[CmdletBinding()]
param(
    [int]$BackendPort = 8080,
    [int]$ResolverAPort = 8001,
    [int]$ResolverBPort = 8002,
    [int]$ResolverCPort = 8003,
    [string]$PythonPath,
    [string]$StatePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $PythonPath) {
    $PythonPath = Join-Path $RepoRoot ".venv\Scripts\python.exe"
}
if (-not $StatePath) {
    $StatePath = Join-Path $RepoRoot ".avdr-demo\session.json"
}

function Get-PortOwners {
    param([int]$Port)

    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $Port `
            -ErrorAction SilentlyContinue |
            Sort-Object OwningProcess -Unique
    )
    return @(
        foreach ($listener in $listeners) {
            $process = Get-CimInstance Win32_Process `
                -Filter ("ProcessId=" + $listener.OwningProcess) `
                -ErrorAction SilentlyContinue
            [pscustomobject]@{
                port = $Port
                pid = $listener.OwningProcess
                name = $process.Name
                command_line = $process.CommandLine
            }
        }
    )
}

function Wait-ForJsonHealth {
    param(
        [string]$Uri,
        [string]$Label,
        [int]$TimeoutSeconds = 30
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-RestMethod -Uri $Uri -TimeoutSec 2
            if ($response.status -eq "ok") {
                return $response
            }
        }
        catch {
            # Startup races are expected until the deadline.
        }
        Start-Sleep -Milliseconds 150
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "$Label did not become healthy within $TimeoutSeconds seconds: $Uri"
}

function Start-AvdrProcess {
    param(
        [string]$Role,
        [string]$Module,
        [int]$Port,
        [hashtable]$Environment
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $PythonPath
    $startInfo.WorkingDirectory = $RepoRoot
    $startInfo.Arguments = (
        "-m uvicorn {0} --host 127.0.0.1 --port {1} --workers 1 --log-level warning" `
            -f $Module, $Port
    )
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.EnvironmentVariables["PYTHONPATH"] = (Join-Path $RepoRoot "src")
    foreach ($name in $Environment.Keys) {
        if ($null -eq $Environment[$name]) {
            $startInfo.EnvironmentVariables.Remove($name)
        }
        else {
            $startInfo.EnvironmentVariables[$name] = [string]$Environment[$name]
        }
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Failed to start $Role"
    }
    $process.Refresh()
    return [pscustomobject]@{
        role = $Role
        pid = $process.Id
        module = $Module
        port = $Port
        executable = (Resolve-Path $PythonPath).Path
        start_time_utc = $process.StartTime.ToUniversalTime().ToString("o")
        process = $process
    }
}

function Save-Session {
    param($Session)

    $stateDirectory = Split-Path -Parent $StatePath
    if (-not (Test-Path -LiteralPath $stateDirectory)) {
        $null = New-Item -ItemType Directory -Path $stateDirectory
    }
    $serializable = [ordered]@{
        schema_version = $Session.schema_version
        repo_root = $Session.repo_root
        created_utc = $Session.created_utc
        dashboard_url = $Session.dashboard_url
        ports = $Session.ports
        processes = @(
            foreach ($entry in $Session.processes) {
                [ordered]@{
                    role = $entry.role
                    pid = $entry.pid
                    module = $entry.module
                    port = $entry.port
                    executable = $entry.executable
                    start_time_utc = $entry.start_time_utc
                }
            }
        )
    }
    $serializable | ConvertTo-Json -Depth 6 |
        Set-Content -LiteralPath $StatePath -Encoding UTF8
}

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python environment not found: $PythonPath. Create .venv and install requirements first."
}
if (Test-Path -LiteralPath $StatePath) {
    throw "Demo session state already exists at $StatePath. Run scripts\stop_demo.ps1 first."
}

$portMap = [ordered]@{
    backend = $BackendPort
    resolver_a = $ResolverAPort
    resolver_b = $ResolverBPort
    resolver_c = $ResolverCPort
}
$uniquePorts = @($portMap.Values | Sort-Object -Unique)
if ($uniquePorts.Count -ne $portMap.Count) {
    throw "Backend and resolver ports must all be distinct."
}
foreach ($entry in $portMap.GetEnumerator()) {
    $owners = @(Get-PortOwners -Port $entry.Value)
    if ($owners.Count -gt 0) {
        $detail = $owners | ConvertTo-Json -Compress
        throw "Required $($entry.Key) port $($entry.Value) is occupied. Refusing to stop or reuse it. Owner: $detail"
    }
}

$session = [ordered]@{
    schema_version = "avdr-demo-session-v1"
    repo_root = $RepoRoot
    created_utc = [DateTime]::UtcNow.ToString("o")
    dashboard_url = "http://127.0.0.1:$BackendPort/dashboard/"
    ports = $portMap
    processes = @()
}

try {
    $resolverSpecs = @(
        @("resolver-a", $ResolverAPort),
        @("resolver-b", $ResolverBPort),
        @("resolver-c", $ResolverCPort)
    )
    foreach ($spec in $resolverSpecs) {
        $resolver = Start-AvdrProcess `
            -Role $spec[0] `
            -Module "avdr.resolver.app:app" `
            -Port $spec[1] `
            -Environment @{
                RESOLVER_ID = $spec[0]
                RESOLVER_PORT = $spec[1]
            }
        $session.processes += $resolver
        Save-Session -Session $session
    }

    foreach ($spec in $resolverSpecs) {
        $null = Wait-ForJsonHealth `
            -Uri ("http://127.0.0.1:{0}/health" -f $spec[1]) `
            -Label $spec[0]
    }

    $backend = Start-AvdrProcess `
        -Role "backend" `
        -Module "avdr.real_router.app:app" `
        -Port $BackendPort `
        -Environment @{
            AVDR_PROVIDER_INVENTORY = $null
            LOCAL_A_URL = "http://127.0.0.1:$ResolverAPort"
            LOCAL_B_URL = "http://127.0.0.1:$ResolverBPort"
            LOCAL_C_URL = "http://127.0.0.1:$ResolverCPort"
        }
    $session.processes += $backend
    Save-Session -Session $session

    $health = Wait-ForJsonHealth `
        -Uri "http://127.0.0.1:$BackendPort/health" `
        -Label "AVDR backend"
    $dashboard = Invoke-WebRequest `
        -UseBasicParsing `
        -Uri $session.dashboard_url `
        -TimeoutSec 5
    if ($dashboard.StatusCode -ne 200) {
        throw "Dashboard returned HTTP $($dashboard.StatusCode)"
    }
    $scenarioStatus = Invoke-RestMethod `
        -Uri "http://127.0.0.1:$BackendPort/demo/scenarios" `
        -TimeoutSec 5
    $scenarioIds = @($scenarioStatus.scenarios | ForEach-Object { $_.id })
    $expected = @("normal", "slow_failure", "fast_unacceptable")
    if (-not $scenarioStatus.available -or ($scenarioIds -join ",") -ne ($expected -join ",")) {
        throw "Controlled scenario inventory is incomplete: $($scenarioIds -join ',')"
    }

    Write-Output "AVDR_DEMO_STATUS=READY"
    Write-Output "DASHBOARD_URL=$($session.dashboard_url)"
    Write-Output "SCENARIOS=$($scenarioIds -join ',')"
    Write-Output "EVIDENCE_MODE=$($scenarioStatus.evidence_mode)"
    Write-Output "ADAPTIVE_AVAILABLE=$($health.adaptive_runtime.adaptive_available)"
    Write-Output "ADAPTIVE_READY_REAL=$($health.adaptive_runtime.adaptive_ready)"
    Write-Output "SESSION_STATE=$StatePath"
    Write-Output "STOP_COMMAND=.\scripts\stop_demo.ps1"
}
catch {
    $cleanupEntries = @($session.processes)
    [array]::Reverse($cleanupEntries)
    foreach ($entry in $cleanupEntries) {
        try {
            if (-not $entry.process.HasExited) {
                Stop-Process -Id $entry.pid -Force -ErrorAction SilentlyContinue
            }
        }
        catch {
            # Preserve the original startup failure below.
        }
    }
    if (Test-Path -LiteralPath $StatePath) {
        Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
    }
    throw
}
