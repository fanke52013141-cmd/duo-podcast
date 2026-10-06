param([string]$ConfigPath = (Join-Path $PSScriptRoot 'runtime.local.json'))
$ErrorActionPreference = 'Stop'
$runtimeConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$engineHome = $runtimeConfig.engineHome
$runtimeRoot = $runtimeConfig.runtimeRoot
$serverRoot = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'apps\server'
$pythonPath = Join-Path $runtimeRoot 'venv\Scripts\python.exe'
$comfyRoot = Join-Path $runtimeRoot 'ComfyUI'
foreach ($requiredPath in @($pythonPath, $comfyRoot, $runtimeConfig.modelRoot, (Join-Path $serverRoot '.venv\Scripts\python.exe'), (Join-Path $serverRoot '_run_dev_toapis.cmd'), (Join-Path $engineHome 'input\reference_A.wav'), (Join-Path $engineHome 'input\reference_B.wav'))) {
    if (-not (Test-Path -LiteralPath $requiredPath)) { throw "Missing runtime dependency: $requiredPath" }
}
Get-Command ffmpeg,ffprobe -ErrorAction Stop | Out-Null
$logRoot = Join-Path $engineHome 'logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$runStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$env:PYTHONIOENCODING = 'utf-8'
$env:DUOCAST_ENGINE_HOME = $engineHome
$env:DUOCAST_COMFY_ROOT = $comfyRoot
$env:PYTHONPATH = Join-Path $runtimeRoot 'venv\Lib\site-packages'
try { Invoke-RestMethod 'http://127.0.0.1:8191/queue' -TimeoutSec 3 | Out-Null; $engineReady = $true } catch { $engineReady = $false }
if (-not $engineReady) {
    Start-Process -FilePath $pythonPath -ArgumentList @(('"' + (Join-Path $PSScriptRoot 'start_guarded.py') + '"')) -WorkingDirectory $comfyRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot "$runStamp-engine.stdout.log") -RedirectStandardError (Join-Path $logRoot "$runStamp-engine.stderr.log") | Out-Null
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        Start-Sleep -Seconds 1
        try { Invoke-RestMethod 'http://127.0.0.1:8191/queue' -TimeoutSec 1 | Out-Null; $engineReady = $true; break } catch { }
    }
    if (-not $engineReady) { throw "Engine did not start; inspect $runStamp-engine.stderr.log" }
}
$env:DUOCAST_VIDEO_PROVIDER = 'comfyui'
$env:DUOCAST_VIDEO_URL = 'http://127.0.0.1:8191'
$env:DUOCAST_VIDEO_INPUT = Join-Path $engineHome 'input'
$env:DUOCAST_TTS_PROVIDER = 'comfyui'
$env:DUOCAST_TEXT_PROVIDER = 'toapis'
$env:DUOCAST_TEXT_MODEL = 'deepseek-flash'
$env:DUOCAST_COMFYUI_URL = 'http://127.0.0.1:8191'
$env:DUOCAST_COMFYUI_INPUT = Join-Path $engineHome 'input'
$env:DUOCAST_TTS_REF_A = Join-Path $engineHome 'input\reference_A.wav'
$env:DUOCAST_TTS_REF_B = Join-Path $engineHome 'input\reference_B.wav'
# The application has a different Python runtime from the model engine.
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
try { Invoke-RestMethod 'http://127.0.0.1:8100/api/providers/capabilities' -TimeoutSec 3 | Out-Null; $appReady = $true } catch { $appReady = $false }
if (-not $appReady) {
    Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c','_run_dev_toapis.cmd') -WorkingDirectory $serverRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot "$runStamp-app.stdout.log") -RedirectStandardError (Join-Path $logRoot "$runStamp-app.stderr.log") | Out-Null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        try { Invoke-RestMethod 'http://127.0.0.1:8100/api/providers/capabilities' -TimeoutSec 1 | Out-Null; $appReady = $true; break } catch { }
    }
    if (-not $appReady) { throw "Application did not start; inspect $runStamp-app.stderr.log" }
}
Write-Output 'DuoCast: http://127.0.0.1:8100 | Engine: http://127.0.0.1:8191'
