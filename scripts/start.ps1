param([ValidateSet('mock','configured')][string]$Mode='configured', [string]$ConfigPath='', [ValidateRange(1,65535)][int]$Port=0, [string]$StorageRoot='', [switch]$CheckOnly)
$ErrorActionPreference='Stop'
$env:PYTHONUTF8='1'
$repoRoot = Split-Path $PSScriptRoot -Parent
$serverRoot = Join-Path $repoRoot 'apps\server'
$pythonPath = Join-Path $serverRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Missing server environment: create apps/server/.venv and pip install -e .[dev]' }
if ($ConfigPath) {
    $localConfig = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    foreach ($property in $localConfig.PSObject.Properties) {
        if ($property.Name -notmatch '^DUOCAST_[A-Z0-9_]+$') { throw 'Only DUOCAST_ environment settings are accepted; keep credentials in the system environment.' }
        [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, 'Process')
    }
}
if ($Port) { $env:DUOCAST_PORT=[string]$Port }
if ($StorageRoot) { $env:DUOCAST_STORAGE=[System.IO.Path]::GetFullPath($StorageRoot) }
if ($Mode -eq 'mock') { $env:DUOCAST_DEMO_ONLY='1' } else { Remove-Item Env:DUOCAST_DEMO_ONLY -ErrorAction SilentlyContinue }
$env:NO_PROXY='127.0.0.1,localhost,::1'
$appPort = if ($env:DUOCAST_PORT) { [int]$env:DUOCAST_PORT } else { 8100 }
if ($Mode -eq 'mock' -or $CheckOnly) {
    & $pythonPath (Join-Path $PSScriptRoot 'check_cpu_environment.py') --port $appPort
    if ($LASTEXITCODE -ne 0) { throw 'Environment check failed; see missing dependencies or port permissions above.' }
    if ($CheckOnly) { return }
}
Write-Host "DuoCast mode=$Mode URL=http://127.0.0.1:$appPort (Ctrl+C to stop)"
Push-Location $serverRoot
try {
    & $pythonPath -m uvicorn duocast.main:app --host 127.0.0.1 --port $appPort
    if ($LASTEXITCODE -ne 0) { throw "Server exited with code $LASTEXITCODE" }
} finally { Pop-Location }
