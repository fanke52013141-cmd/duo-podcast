param([string]$ConfigPath = (Join-Path $PSScriptRoot 'runtime.local.json'))
$ErrorActionPreference = 'Stop'
$runtimeConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$engineHome = $runtimeConfig.engineHome
$runtimeRoot = $runtimeConfig.runtimeRoot
$serverRoot = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'apps\server'
$pythonPath = Join-Path $runtimeRoot 'venv\Scripts\python.exe'
$comfyRoot = Join-Path $runtimeRoot 'ComfyUI'
foreach ($requiredPath in @($pythonPath, $comfyRoot, $runtimeConfig.modelRoot, (Join-Path $serverRoot '.venv\Scripts\python.exe'), (Join-Path $engineHome 'input\reference_A.wav'), (Join-Path $engineHome 'input\reference_B.wav'))) {
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
if ($runtimeConfig.fastEngineRoot) { $env:DUOCAST_FAST_ENGINE = $runtimeConfig.fastEngineRoot }
$env:DUOCAST_VIDEO_PROVIDER = 'comfyui'
$env:DUOCAST_VIDEO_URL = 'http://127.0.0.1:8191'
# 44号标准模式加速胜出组合（2026-11-13 实测 29s 片 79.1min→24.5min，3.23x，画质/口型已验证）。
# 块交换/预取在本机无收益、torch.compile 因 inductor 循环导入不可用，均保持默认。见 44 号文档第五节。
if (-not $env:DUOCAST_VIDEO_ATTENTION) { $env:DUOCAST_VIDEO_ATTENTION = 'sageattn' }
if (-not $env:DUOCAST_VIDEO_TEXT_CFG) { $env:DUOCAST_VIDEO_TEXT_CFG = '1.0' }
if (-not $env:DUOCAST_VIDEO_AUDIO_CFG) { $env:DUOCAST_VIDEO_AUDIO_CFG = '1.0' }
# 47号背景退化修复（2026-11-13 实测 EP9930 29s：背景偏离趋势比 1.87→0.89，末帧背景与首帧同级）。
# clean=同框每段重锚定干净底图，打断参考帧链式累积退化；仅作用同框路径，过肩不受影响。
# 回退：设 DUOCAST_ANCHOR_MODE=chain 重启即恢复旧行为。见 47 号文档。
if (-not $env:DUOCAST_ANCHOR_MODE) { $env:DUOCAST_ANCHOR_MODE = 'clean' }
# 导出 AI 超分（SeedVR2-3B FP8）：默认关闭——本机约 2.5h/2分钟片，渲染页"AI 超分"选项可按次开启，
# 也可设 DUOCAST_EXPORT_SR=1 全局默认开。实测画质见 44 号文档第十节。
# DUOCAST_SEEDVR2_PYTHON 须指向含 torch/sageattention 的引擎 venv（开启超分时使用）。
if (-not $env:DUOCAST_SEEDVR2_PYTHON) { $env:DUOCAST_SEEDVR2_PYTHON = 'D:\PPT_Studio_Assets\InfiniteTalk_TTS\InfiniteTalk_Runtime\venv\Scripts\python.exe' }
$env:DUOCAST_VIDEO_INPUT = Join-Path $engineHome 'input'
$env:DUOCAST_TTS_PROVIDER = 'comfyui'
$env:DUOCAST_TEXT_PROVIDER = 'toapis'
$env:DUOCAST_IMAGE_PROVIDER = 'toapis'
$env:DUOCAST_TEXT_MODEL = 'deepseek-flash'
$env:DUOCAST_COMFYUI_URL = 'http://127.0.0.1:8191'
$env:DUOCAST_COMFYUI_INPUT = Join-Path $engineHome 'input'
$env:DUOCAST_TTS_REF_A = Join-Path $engineHome 'input\reference_A.wav'
$env:DUOCAST_TTS_REF_B = Join-Path $engineHome 'input\reference_B.wav'
# The application has a different Python runtime from the model engine.
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
try { Invoke-RestMethod 'http://127.0.0.1:8100/api/providers/capabilities' -TimeoutSec 3 | Out-Null; $appReady = $true } catch { $appReady = $false }
if (-not $appReady) {
    Start-Process -FilePath (Join-Path $serverRoot '.venv\Scripts\python.exe') -ArgumentList @('-m','uvicorn','duocast.main:app','--host','127.0.0.1','--port','8100') -WorkingDirectory $serverRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot "$runStamp-app.stdout.log") -RedirectStandardError (Join-Path $logRoot "$runStamp-app.stderr.log") | Out-Null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        try { Invoke-RestMethod 'http://127.0.0.1:8100/api/providers/capabilities' -TimeoutSec 1 | Out-Null; $appReady = $true; break } catch { }
    }
    if (-not $appReady) { throw "Application did not start; inspect $runStamp-app.stderr.log" }
}
Write-Output 'DuoCast: http://127.0.0.1:8100 | Engine: http://127.0.0.1:8191'
