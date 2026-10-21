$ErrorActionPreference = 'Stop'
$endpointLine = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'private\calculator.env') | Where-Object { $_ -match '^MCP_ENDPOINT=' } | Select-Object -First 1
if (-not $endpointLine) { throw '请先运行 Configure-Endpoint.ps1。' }
$previousEndpoint = $env:MCP_ENDPOINT
try {
    $env:MCP_ENDPOINT = ($endpointLine -split '=', 2)[1]
    Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'verify_backend.py') | docker exec -i -e MCP_ENDPOINT xiaozhi-esp32-server python -
    if ($LASTEXITCODE -ne 0) { throw '小智后端 MCP 调用验证失败。' }
} finally {
    $env:MCP_ENDPOINT = $previousEndpoint
}
