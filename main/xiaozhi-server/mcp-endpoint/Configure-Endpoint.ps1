param(
    [string]$EndpointHost = '172.17.230.9',
    [string]$AgentId = '8263ee79da85441999e486f03f622dd4'
)

$ErrorActionPreference = 'Stop'
$privateDirectory = Join-Path $PSScriptRoot 'private'
New-Item -ItemType Directory -Path $privateDirectory -Force | Out-Null
$configPath = Join-Path $PSScriptRoot 'data\.mcp-endpoint-server.cfg'
$keyLine = Get-Content -LiteralPath $configPath | Where-Object { $_ -match '^key\s*=' } | Select-Object -First 1
if (-not $keyLine) { throw '接入点配置中缺少 key。' }
$endpointKey = ($keyLine -split '=', 2)[1].Trim()

# Reuse the administrator's current login; do not create accounts or change passwords.
$sessionToken = (& docker exec xiaozhi-esp32-server-db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -D "$MYSQL_DATABASE" -N -e "SELECT t.token FROM sys_user_token t JOIN sys_user u ON u.id=t.user_id WHERE t.expire_date>NOW() AND u.super_admin=1 LIMIT 1;"').Trim()
if ($LASTEXITCODE -ne 0 -or -not $sessionToken) { throw '请先在智控台登录管理员账号，再运行此脚本。' }
$authHeaders = @{ Authorization = 'Bearer ' + $sessionToken }
$apiBase = 'http://127.0.0.1:8002/xiaozhi'

function Invoke-AdminApi([string]$Path, [string]$Method = 'GET', $Body = $null) {
    $arguments = @{ Uri = $apiBase + $Path; Method = $Method; Headers = $authHeaders; TimeoutSec = 30 }
    if ($null -ne $Body) {
        $arguments.ContentType = 'application/json; charset=utf-8'
        $arguments.Body = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 30 -Compress))
    }
    $response = Invoke-RestMethod @arguments
    if ($response.code -ne 0) { throw ('管理接口失败：' + $response.msg) }
    return $response.data
}

$endpointParam = Invoke-AdminApi '/admin/params/113'
$menuParam = Invoke-AdminApi '/admin/params/600'
$backupPath = Join-Path $privateDirectory 'setup-backup.json'
if (-not (Test-Path -LiteralPath $backupPath)) {
    @{ endpoint = $endpointParam; menu = $menuParam } | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $backupPath -Encoding utf8
}

$endpointParam.paramValue = 'http://' + $EndpointHost + ':8005/mcp_endpoint/health?key=' + $endpointKey
$null = Invoke-AdminApi '/admin/params' 'PUT' $endpointParam
$menuConfig = $menuParam.paramValue | ConvertFrom-Json
if (-not $menuConfig.features.mcpAccessPoint) { throw '系统功能配置缺少 mcpAccessPoint。' }
$menuConfig.features.mcpAccessPoint.enabled = $true
$menuParam.paramValue = $menuConfig | ConvertTo-Json -Depth 30 -Compress
$null = Invoke-AdminApi '/admin/params' 'PUT' $menuParam

$toolEndpoint = Invoke-AdminApi ('/agent/mcp/address/' + $AgentId)
if (-not $toolEndpoint.StartsWith('ws')) { throw '未获取有效 MCP 接入地址。' }
Set-Content -LiteralPath (Join-Path $privateDirectory 'calculator.env') -Encoding utf8 -Value ('MCP_ENDPOINT=' + $toolEndpoint)
@{ agentId = $AgentId; endpointHost = $EndpointHost } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $privateDirectory 'deployment.json') -Encoding utf8
Write-Output '智控台 MCP 参数、功能开关和计算器连接配置已保存。'
Write-Output '现在可运行 docker compose up -d。'
