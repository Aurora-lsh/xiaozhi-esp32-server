param(
    [string]$SdkPath,
    [string]$PythonEnvironment,
    [ValidateRange(1, 4)][int]$Jobs = 1
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$runtime = Join-Path $taskRoot '.runtime'
$source = Join-Path $runtime 'otto-firmware-source'
$baseCommit = 'b6c61fe390dcc66d73f61d797c7ca499e02106d1'
$patch = Join-Path $PSScriptRoot 'otto-2.2.2-readable.patch'
if (-not $SdkPath) { $SdkPath = Join-Path $runtime 'esp-idf-5.5.2' }
if (-not $PythonEnvironment) {
    $PythonEnvironment = Join-Path $env:USERPROFILE '.espressif\python_env\idf5.5_py3.11_env'
}
if (-not (Test-Path (Join-Path $SdkPath 'tools\idf.py'))) { throw 'Install ESP-IDF 5.5.2 first, then pass -SdkPath.' }
if (-not (Test-Path (Join-Path $PythonEnvironment 'Scripts\python.exe'))) { throw 'Pass the ESP-IDF Python environment with -PythonEnvironment.' }
if (-not (Test-Path $source)) {
    git clone --no-checkout https://github.com/txp666/xiaozhi-esp32.git $source
    if ($LASTEXITCODE -ne 0) { throw 'Unable to download firmware source' }
    git -C $source checkout --detach $baseCommit
    if ($LASTEXITCODE -ne 0) { throw 'Unable to select firmware 2.2.2' }
}
$head = (& git -C $source rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $head -ne $baseCommit) { throw 'Firmware source is not the pinned 2.2.2 revision; existing files were preserved.' }
try {
    git -C $source apply --reverse --check $patch 2>$null
    $alreadyApplied = $LASTEXITCODE -eq 0
} catch { $alreadyApplied = $false }
if (-not $alreadyApplied) {
    git -C $source apply --check $patch
    if ($LASTEXITCODE -ne 0) { throw 'Patch differs from local changes; inspect the source before applying.' }
    git -C $source apply $patch
    if ($LASTEXITCODE -ne 0) { throw 'Unable to apply display patch' }
}
$env:IDF_PATH = (Resolve-Path $SdkPath).Path
$env:IDF_TOOLS_PATH = Join-Path $env:USERPROFILE '.espressif'
$env:IDF_PYTHON_ENV_PATH = (Resolve-Path $PythonEnvironment).Path
$env:PATH = "$env:IDF_PYTHON_ENV_PATH\Scripts;$env:PATH"
$env:PYTHONPATH = Join-Path $runtime 'idf-python-extra'
$env:IDF_COMPONENT_CACHE_PATH = Join-Path $runtime 'idf-component-cache'
$env:CMAKE_BUILD_PARALLEL_LEVEL = [string]$Jobs
$env:CMAKE_NINJA_FORCE_RESPONSE_FILE = '1'
$env:PYTHONUTF8 = '1'
. (Join-Path $env:IDF_PATH 'export.ps1')
$output = Join-Path $runtime 'voice-streaming-20261020'
New-Item -ItemType Directory -Path $output -Force | Out-Null
Push-Location $source
try {
    $runner = @('scripts/release.py', 'otto-robot', '--name', 'otto-robot-nocam-readable')
    $configurationPath = Join-Path $source 'sdkconfig'
    if ((Test-Path (Join-Path $source 'build\build.ninja')) -and (Test-Path $configurationPath)) {
        $configuration = Get-Content -LiteralPath $configurationPath -Raw
        $sameVariant = @('CONFIG_IDF_TARGET_ESP32S3=y', 'CONFIG_BOARD_TYPE_OTTO_ROBOT=y',
            'CONFIG_OTTO_NO_CAMERA=y', 'CONFIG_OTTO_READABLE_DISPLAY=y', 'CONFIG_FLASH_NONE_ASSETS=y') |
            Where-Object { $configuration -notmatch ('(?m)^' + [regex]::Escape($_) + '\r?$') }
        if (@($sameVariant).Count -eq 0) {
            Write-Host 'Reusing the configured no-camera build for incremental compilation.'
            $runner = @((Join-Path $env:IDF_PATH 'tools\idf.py'), '-DBOARD_NAME=otto-robot-nocam-readable',
                '-DBOARD_TYPE=otto-robot', '-DCMAKE_NINJA_FORCE_RESPONSE_FILE=ON', "-DCMAKE_JOB_POOLS=release_pool=$Jobs",
                '-DCMAKE_JOB_POOL_COMPILE=release_pool', '-DCMAKE_JOB_POOL_LINK=release_pool', 'build')
        }
    }
    try {
        $ErrorActionPreference = 'Continue'
        python @runner 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath (Join-Path $output 'firmware-build.log')
        $buildExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = 'Stop' }
    if ($buildExit -ne 0) { throw 'Firmware build failed; inspect firmware-build.log.' }
    $app = Join-Path $source 'build\xiaozhi.bin'
    if (-not (Test-Path $app)) { throw 'Build did not produce xiaozhi.bin.' }
    if ((Get-Item $app).Length -gt 0x3f0000) { throw 'Application exceeds the existing OTA partition.' }
    Copy-Item -LiteralPath $app -Destination (Join-Path $output 'otto-nocam-readable-app.bin')
    [ordered]@{
        source = 'https://github.com/txp666/xiaozhi-esp32.git'
        commit = $baseCommit
        variant = 'otto-robot-nocam-readable'
        appSha256 = (Get-FileHash -LiteralPath $app -Algorithm SHA256).Hash.ToLower()
        appBytes = (Get-Item $app).Length
        buildJobs = $Jobs
        builtAt = (Get-Date).ToString('s')
        flashed = $false
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'firmware-build.json') -Encoding UTF8
    Write-Host 'Application built. No device has been flashed.'
} finally { Pop-Location }
