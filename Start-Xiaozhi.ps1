# Start the existing Windows/Docker deployment from this checkout.
[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$CheckOnly,
    [switch]$SkipMcp,
    [switch]$SkipLlm,
    [switch]$OpenPages,
    [ValidateRange(10, 900)][int]$TimeoutSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$serverRoot = Join-Path $PSScriptRoot 'main\xiaozhi-server'
$ttsRoot = Join-Path $serverRoot 'audio_ref\spongebob'
$script:LocalLlmModel = 'qwen/qwen3.5-9b'
$script:LocalLlmContext = 16384
$mainCompose = @('compose', '-f', (Join-Path $serverRoot 'docker-compose_all.yml'),
    '-f', (Join-Path $serverRoot 'docker-compose.memory.yml'))
$coreContainers = @('xiaozhi-esp32-server-db', 'xiaozhi-esp32-server-redis',
    'xiaozhi-esp32-server-web', 'xiaozhi-esp32-server')
$mcpContainers = @('xiaozhi-local-mcp', 'xiaozhi-mcp-endpoint', 'xiaozhi-mcp-calculator')
$dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
if (-not $dockerCommand) { throw 'Docker CLI was not found. Install/start Docker Desktop first.' }
$script:DockerExe = $dockerCommand.Source

function Invoke-Docker {
    param([string[]]$DockerArguments)
    & $script:DockerExe @DockerArguments
    if ($LASTEXITCODE -ne 0) { throw ('Docker command failed: docker ' + ($DockerArguments -join ' ')) }
}

function Test-DockerReady {
    try {
        $null = & $script:DockerExe info --format '{{.ServerVersion}}' 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Get-DockerWslDataPath {
    param([string]$SettingsPath = (Join-Path $env:APPDATA 'Docker\settings-store.json'))
    if (-not (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) { return $null }
    $settings = Get-Content -Raw -LiteralPath $SettingsPath -Encoding UTF8 | ConvertFrom-Json
    $dataDirectory = $settings.PSObject.Properties['CustomWslDistroDir']
    if ($null -eq $dataDirectory -or [string]::IsNullOrWhiteSpace([string]$dataDirectory.Value)) {
        return $null
    }
    return [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables([string]$dataDirectory.Value))
}

function Get-DockerDesktopPath {
    # The Desktop executable follows the CLI installation, not the WSL disk.
    $cliDirectory = Split-Path -Parent $script:DockerExe
    $desktopPaths = @(
        (Join-Path $cliDirectory '..\..\Docker Desktop.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'),
        (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe')
    )
    $desktop = $desktopPaths | Where-Object {
        Test-Path -LiteralPath $_ -PathType Leaf
    } | Select-Object -First 1
    if (-not $desktop) { throw 'Docker Desktop was not found. Open Docker Desktop manually, then retry.' }
    return (Resolve-Path -LiteralPath $desktop).ProviderPath
}

function Test-LocalImage {
    try {
        $null = & $script:DockerExe image inspect 'xiaozhi-esp32-server:local-memory' 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Wait-Ready {
    param([string]$Description, [scriptblock]$Probe)
    Write-Host ('Waiting for ' + $Description + ' ...')
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (& $Probe) { Write-Host ('OK: ' + $Description) -ForegroundColor Green; return }
        Start-Sleep -Seconds 3
    } while ([DateTime]::UtcNow -lt $deadline)
    throw ('Timed out waiting for ' + $Description + '. Check the service logs listed in docs/windows-startup.md.')
}

function Test-ContainerReady {
    param([string]$Name)
    try {
        $raw = & $script:DockerExe inspect --format '{{json .State}}' $Name 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $state = ($raw -join '') | ConvertFrom-Json
        if (-not $state.Running) { return $false }
        if ($state.PSObject.Properties['Health']) { return $state.Health.Status -eq 'healthy' }
        return $true
    } catch { return $false }
}

function Get-LocalHttpBody {
    param([string]$Uri)
    $request = [System.Net.HttpWebRequest]::Create($Uri)
    $request.Proxy = $null
    $request.Timeout = 4000
    $request.ReadWriteTimeout = 4000
    $response = $null
    $reader = $null
    try {
        $response = $request.GetResponse()
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        return $reader.ReadToEnd()
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Close() }
    }
}

function Test-ManagerReady {
    try {
        $body = Get-LocalHttpBody 'http://127.0.0.1:8002/xiaozhi/user/pub-config'
        $result = $body | ConvertFrom-Json
        return $result.code -eq 0
    } catch { return $false }
}

function Test-MemoryReady {
    try { return (Get-LocalHttpBody 'http://127.0.0.1:8003/memory/').Contains('/memory/app.js') }
    catch { return $false }
}

function Test-WebsocketPort {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync('127.0.0.1', 8000)
        return $task.Wait(1500) -and $client.Connected
    } catch { return $false }
    finally { $client.Dispose() }
}

function Test-LocalMcpReady {
    try { return ((Get-LocalHttpBody 'http://127.0.0.1:8004/health') | ConvertFrom-Json).status -eq 'ok' }
    catch { return $false }
}

function Test-LocalTtsReady {
    try {
        $api = (Get-LocalHttpBody 'http://127.0.0.1:9880/openapi.json') | ConvertFrom-Json
        $route = $api.paths.PSObject.Properties['/tts']
        return $null -ne $route -and $null -ne $route.Value.PSObject.Properties['post'] -and
            $null -ne $api.paths.PSObject.Properties['/set_sovits_weights']
    } catch { return $false }
}

function Start-LocalTts {
    if (Test-LocalTtsReady) {
        Write-Host 'Reusing the running GPT-SoVITS SpongeBob TTS service (9880).'
        return
    }
    $ttsPython = Join-Path $PSScriptRoot '.runtime\gpt-sovits-venv\Scripts\python.exe'
    # A busy inference request can also delay /openapi.json. Check the actual
    # process even if an old PID file was lost, before loading another model.
    $runningTts = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.ExecutablePath -eq $ttsPython -and
            $_.CommandLine -match 'api_v2[.]py' -and $_.CommandLine -match 'spongebob[.]yaml' })
    if ($runningTts.Count -gt 0) {
        Write-Host 'GPT-SoVITS is already starting or busy; waiting for its API.'
        return
    }
    foreach ($file in @(
        (Join-Path $ttsRoot 'start_tts.ps1'),
        $ttsPython,
        (Join-Path $PSScriptRoot '.runtime\gpt-sovits\api_v2.py'),
        (Join-Path $PSScriptRoot '.runtime\gpt-sovits\GPT_SoVITS\configs\spongebob.yaml')
    )) {
        if (-not (Test-Path -LiteralPath $file)) { throw ('Missing local TTS runtime file: ' + $file) }
    }
    # The existing helper checks its PID before launching, runs hidden, and
    # sets the local/offline model environment. Isolate its exit/env changes.
    Write-Host 'Starting GPT-SoVITS SpongeBob TTS ...'
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ttsRoot 'start_tts.ps1')
    if ($LASTEXITCODE -ne 0) { throw ('Could not start local TTS. Check logs in ' + $ttsRoot) }
}

function Test-BackendTtsReady {
    try {
        $probe = "import json,urllib.request; opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); response=opener.open('http://host.docker.internal:9880/openapi.json',timeout=4); api=json.load(response); assert 'post' in api['paths']['/tts'] and '/set_sovits_weights' in api['paths']"
        $null = & $script:DockerExe exec xiaozhi-esp32-server python -c $probe 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Get-LocalLlmModels {
    return ((Get-LocalHttpBody 'http://127.0.0.1:1234/api/v0/models') | ConvertFrom-Json).data
}

function Test-LocalLlmReady {
    try {
        $models = @(Get-LocalLlmModels)
        return @($models | Where-Object { $_.id -eq $script:LocalLlmModel -and
            $_.state -eq 'loaded' -and $_.loaded_context_length -eq $script:LocalLlmContext }).Count -gt 0
    } catch { return $false }
}

function Invoke-Lms {
    param([string[]]$LmsArguments)
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $script:LmsExe
    # These arguments are fixed flags, numbers and a space-free model identifier.
    $info.Arguments = $LmsArguments -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill()
            throw 'LM Studio command timed out. Check LM Studio before retrying.'
        }
        $null = $stdout.GetAwaiter().GetResult()
        $null = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw ('LM Studio command failed: lms ' + ($LmsArguments -join ' ')) }
    } finally { $process.Dispose() }
}

function Start-LocalLlm {
    if (Test-LocalLlmReady) {
        Write-Host 'Reusing LM Studio Qwen (16K context, 1234).'
        return
    }
    $script:LmsExe = Join-Path $env:USERPROFILE '.lmstudio\bin\lms.exe'
    if (-not (Test-Path -LiteralPath $script:LmsExe)) {
        throw 'LM Studio CLI was not found. Open LM Studio, or use -SkipLlm with a different model service.'
    }
    $models = @()
    try { $models = @(Get-LocalLlmModels) }
    catch { Invoke-Lms -LmsArguments @('server', 'start', '--port', '1234') }
    foreach ($model in $models) {
        if ($model.id -eq $script:LocalLlmModel -and $model.state -eq 'loaded') {
            Invoke-Lms -LmsArguments @('unload', $script:LocalLlmModel)
        }
    }
    Write-Host 'Loading local Qwen: 16K context, one prediction, 65% GPU offload ...'
    Invoke-Lms -LmsArguments @('load', $script:LocalLlmModel, '--context-length',
        [string]$script:LocalLlmContext, '--parallel', '1', '--gpu', '0.65', '--yes')
}

function Show-NetworkNote {
    $savedPath = Join-Path $serverRoot 'mcp-endpoint\private\deployment.json'
    if (-not (Test-Path -LiteralPath $savedPath)) { return }
    try {
        $saved = Get-Content -Raw -LiteralPath $savedPath | ConvertFrom-Json
        $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Select-Object -ExpandProperty IPAddress)
        $parsedAddress = $null
        if ([System.Net.IPAddress]::TryParse($saved.endpointHost, [ref]$parsedAddress) -and
            $addresses -notcontains $saved.endpointHost) {
            Write-Warning ('The saved MCP endpoint IP is no longer assigned to this laptop: ' + $saved.endpointHost)
            Write-Warning 'See docs/windows-startup.md for the Wi-Fi/IP change procedure.'
        }
    } catch { Write-Warning 'Could not check the saved MCP endpoint IP. Check it if endpoint health fails.' }
}

try {
    if ($Build -and $CheckOnly) { throw 'Use -Build or -CheckOnly, not both.' }
    foreach ($file in @('docker-compose_all.yml', 'docker-compose.memory.yml', 'data\.config.yaml')) {
        if (-not (Test-Path -LiteralPath (Join-Path $serverRoot $file))) { throw ('Missing file: ' + $file) }
    }
    # Docker owns the disk location. Follow its saved setting after a move;
    # Compose bind mounts continue to point at this checkout's project files.
    $dockerWslDataPath = Get-DockerWslDataPath
    if ($dockerWslDataPath) {
        if (-not (Test-Path -LiteralPath $dockerWslDataPath -PathType Container)) {
            throw ('Docker WSL data directory is unavailable: ' + $dockerWslDataPath +
                '. Check the drive and Docker Desktop Settings > Resources > Advanced.')
        }
        Write-Host ('Docker WSL data: ' + $dockerWslDataPath)
    }
    if (-not (Test-DockerReady)) {
        if ($CheckOnly) { throw 'Docker Engine is not running. Start Docker Desktop, then retry.' }
        $desktop = Get-DockerDesktopPath
        Write-Host ('Starting Docker Desktop: ' + $desktop)
        Start-Process -FilePath $desktop -WindowStyle Hidden | Out-Null
        Wait-Ready 'Docker Engine' { Test-DockerReady }
    }

    if (-not $CheckOnly) {
        Invoke-Docker -DockerArguments ($mainCompose + @('up', '-d', 'xiaozhi-esp32-server-db',
            'xiaozhi-esp32-server-redis', 'xiaozhi-esp32-server-web'))
    }
    Wait-Ready 'MySQL' { Test-ContainerReady 'xiaozhi-esp32-server-db' }
    Wait-Ready 'Redis' { Test-ContainerReady 'xiaozhi-esp32-server-redis' }
    Wait-Ready 'manager API (8002)' { Test-ManagerReady }

    if (-not $SkipMcp) {
        Show-NetworkNote
        if (-not $CheckOnly) {
            foreach ($folder in @('local-mcp', 'mcp-endpoint')) {
                $directory = Join-Path $serverRoot $folder
                if (-not (Test-Path -LiteralPath (Join-Path $directory 'docker-compose.yml'))) {
                    throw ('Missing MCP deployment: ' + $directory + '. Use -SkipMcp for the core backend only.')
                }
                Push-Location -LiteralPath $directory
                try { Invoke-Docker -DockerArguments @('compose', 'up', '-d') }
                finally { Pop-Location }
            }
        }
    }

    if (-not $CheckOnly) {
        Start-LocalTts
        if (-not $SkipLlm) {
            # Let TTS reserve its GPU memory before loading the chat model.
            Wait-Ready 'GPT-SoVITS before loading the chat model' { Test-LocalTtsReady }
            Start-LocalLlm
        }
        $needsBuild = -not (Test-LocalImage)
        if ($Build -or $needsBuild) {
            Invoke-Docker -DockerArguments ($mainCompose + @('build', 'xiaozhi-esp32-server'))
        }
        Invoke-Docker -DockerArguments ($mainCompose + @('up', '-d', '--no-build', '--no-deps', 'xiaozhi-esp32-server'))
    }
    Wait-Ready 'Python backend container' { Test-ContainerReady 'xiaozhi-esp32-server' }
    Wait-Ready 'WebSocket port (8000)' { Test-WebsocketPort }
    Wait-Ready 'memory management page (8003)' { Test-MemoryReady }
    Wait-Ready 'GPT-SoVITS SpongeBob TTS API (9880)' { Test-LocalTtsReady }
    Wait-Ready 'TTS access from the Python backend' { Test-BackendTtsReady }
    if (-not $SkipLlm) {
        Wait-Ready 'LM Studio Qwen with 16K context (1234)' { Test-LocalLlmReady }
    }
    if (-not $SkipMcp) {
        Wait-Ready 'local MCP (8004)' { Test-LocalMcpReady }
        Wait-Ready 'MCP endpoint' { Test-ContainerReady 'xiaozhi-mcp-endpoint' }
        Wait-Ready 'MCP calculator tool discovery' { Test-ContainerReady 'xiaozhi-mcp-calculator' }
    }
    $containers = $coreContainers
    if (-not $SkipMcp) { $containers += $mcpContainers }
    foreach ($container in $containers) {
        if (-not (Test-ContainerReady $container)) { throw ('Container is not ready: ' + $container) }
    }
    if (-not (Test-LocalTtsReady)) { throw 'Local TTS is not ready. Check the TTS logs.' }
    Write-Host ''
    Write-Host 'All selected backends are ready.' -ForegroundColor Green
    Write-Host 'Control panel: http://localhost:8002/'
    Write-Host 'Memory:       http://localhost:8003/memory/'
    Write-Host 'Speech TTS:   http://127.0.0.1:9880/docs (GPT-SoVITS / SpongeBob)'
    if (-not $SkipLlm) { Write-Host 'Chat model:   http://127.0.0.1:1234/v1 (Qwen / 16K context)' }
    Write-Host 'Keep Docker Desktop running. This PowerShell window can now be closed.'
    Write-Host ('Keep the background TTS Python process running. Logs: ' + $ttsRoot)
    if (-not $SkipLlm) { Write-Host 'Keep LM Studio and its loaded model running.' }
    Invoke-Docker -DockerArguments @('ps', '--filter', 'name=xiaozhi', '--format', 'table {{.Names}}\t{{.Status}}\t{{.Ports}}')
    if ($OpenPages) {
        Start-Process 'http://localhost:8002/'
        Start-Process 'http://localhost:8003/memory/'
    }
} catch {
    Write-Host ('Startup/check failed: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Details: docs/windows-startup.md'
    exit 1
}
