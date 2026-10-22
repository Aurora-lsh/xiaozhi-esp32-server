# Run with Windows PowerShell 5.1; no Pester, Docker, or network is required.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $taskRoot 'Start-Xiaozhi.ps1'), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw ($parseErrors | Out-String) }
foreach ($function in $ast.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst]
}, $true)) {
    . ([scriptblock]::Create($function.Extent.Text))
}

Add-Type -TypeDefinition @'
using System.Net;
public class StartupTestResponse : WebResponse {
    public HttpStatusCode StatusCode { get { return HttpStatusCode.BadGateway; } }
    public bool Closed;
    public override void Close() { Closed = true; }
}
'@

$script:DockerExe = 'Invoke-FakeDocker'
$script:ManagerProbeDetail = ''
$script:ManagerHttpStatus = 0
$script:ManagerRecoveryAttempted = $false
$script:FakeHttp = 'ready'
$script:FakeJava = $true
$script:FakeDockerAvailable = $true
$script:Restarts = 0
$script:SleepCalls = 0
$CheckOnly = $false
$TimeoutSeconds = 0
$script:Assertions = 0

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw ('FAILED: ' + $Message) }
    $script:Assertions++
}

function Invoke-FakeDocker {
    $global:LASTEXITCODE = 0
    if (-not $script:FakeDockerAvailable) { $global:LASTEXITCODE = 1; return }
    if ($args[0] -eq 'top') {
        'COMMAND'
        'nginx'
        if ($script:FakeJava) { 'java' }
    } elseif ($args[0] -eq 'restart') {
        $script:Restarts++
        $script:FakeJava = $true
        'xiaozhi-esp32-server-web'
    } else { throw 'Unexpected fake Docker command' }
}

function Get-LocalHttpBody {
    param([string]$Uri)
    if ($script:FakeHttp -eq 'ready') { return '{"code":0}' }
    if ($script:FakeHttp -eq 'application-error') { return '{"code":500}' }
    if ($script:FakeHttp -eq 'invalid-json') { return 'not json' }
    if ($script:FakeHttp -eq 'connection-error') { throw 'Connection refused' }
    $script:FakeResponse = New-Object StartupTestResponse
    $failure = New-Object Net.WebException -ArgumentList @(
        'Bad gateway', $null, [Net.WebExceptionStatus]::ProtocolError, $script:FakeResponse)
    if ($script:FakeHttp -eq 'wrapped-502') {
        throw (New-Object InvalidOperationException -ArgumentList 'Wrapped HTTP failure', $failure)
    }
    throw $failure
}

function Start-Sleep { $script:SleepCalls++ }

Assert-True (Test-ManagerReady) 'A code-zero API is ready'
Assert-True ((Get-ManagerJavaState) -eq 'running') 'Java process is detected'
$script:FakeJava = $false
Assert-True ((Get-ManagerJavaState) -eq 'exited') 'Nginx without Java is detected'
$script:FakeDockerAvailable = $false
Assert-True ((Get-ManagerJavaState) -eq 'unknown') 'Unavailable Docker is not mistaken for a dead Java process'
$script:FakeDockerAvailable = $true

foreach ($mode in @('502', 'wrapped-502')) {
    $script:FakeHttp = $mode
    Assert-True (-not (Test-ManagerReady)) 'A 502 API is not ready'
    Assert-True ($script:ManagerHttpStatus -eq 502) 'HTTP status survives wrapped .NET errors'
    Assert-True $script:FakeResponse.Closed 'Failed HTTP responses are closed'
}

# Reproduce the real cold-start failure: Nginx survives after Java exited.
$script:FakeHttp = '502'
$result = Test-ManagerStartup
Assert-True (($result -is [bool]) -and -not $result) 'Restart output cannot accidentally mark the API ready'
Assert-True ($script:Restarts -eq 1) 'A dead Java API is restarted once'
Assert-True $script:ManagerRecoveryAttempted 'Recovery is remembered for this startup'
Assert-True (-not (Test-ManagerStartup)) 'A live Java process still starting is allowed to finish'
Assert-True ($script:Restarts -eq 1) 'A starting Java process is not restarted again'
$script:FakeHttp = 'ready'
Assert-True (Test-ManagerStartup) 'Recovery completes when the real API becomes ready'

$script:FakeHttp = '502'
$script:FakeJava = $false
$failed = $false
try { $null = Test-ManagerStartup } catch { $failed = $_.Exception.Message -match 'exited again' }
Assert-True $failed 'A second Java exit fails with a diagnostic instead of looping'
Assert-True ($script:Restarts -eq 1) 'Recovery never becomes an unbounded restart loop'

$CheckOnly = $true
$script:ManagerRecoveryAttempted = $false
$failed = $false
try { $null = Test-ManagerStartup } catch { $failed = $_.Exception.Message -match 'Java API has exited' }
Assert-True $failed 'Read-only checks explain a dead Java API'
Assert-True ($script:Restarts -eq 1) 'Read-only checks do not restart containers'
$CheckOnly = $false

foreach ($mode in @('application-error', 'invalid-json', 'connection-error')) {
    $script:FakeHttp = $mode
    Assert-True (-not (Test-ManagerStartup)) 'Unrelated API failures are not blindly repaired'
    Assert-True ($script:Restarts -eq 1) 'No restart occurs without proof Java exited behind Nginx'
}

$script:ManagerProbeDetail = 'HTTP 502: inspect Java logs'
$failed = $false
try { Wait-Ready 'manager API (8002)' { $false } { $script:ManagerProbeDetail } }
catch { $failed = $_.Exception.Message -match 'HTTP 502: inspect Java logs' }
Assert-True $failed 'Timeouts include the current API failure reason'
$script:SleepCalls = 0
Wait-Ready 'ready service' { $true }
Assert-True ($script:SleepCalls -eq 0) 'Ready services do not sleep or mutate state'

Write-Host ('PASS: ' + $script:Assertions + ' startup assertions; no real Docker or network calls.')
