param([ValidateSet('mock','configured')][string]$Mode='configured', [string]$ConfigPath='')
$ErrorActionPreference='Stop'
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
if ($Mode -eq 'mock') { $env:DUOCAST_DEMO_ONLY='1' } else { Remove-Item Env:DUOCAST_DEMO_ONLY -ErrorAction SilentlyContinue }
$env:NO_PROXY='127.0.0.1,localhost,::1'
Push-Location $serverRoot
$appPort = if ($env:DUOCAST_PORT) { [int]$env:DUOCAST_PORT } else { 8100 }
try { & $pythonPath -m uvicorn duocast.main:app --host 127.0.0.1 --port $appPort } finally { Pop-Location }
