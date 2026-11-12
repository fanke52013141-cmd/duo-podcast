# 43 号方案批 A · RTX 5080（Blackwell sm_120）LongCat 引擎环境对齐检查
# 用法（在 5080 机器的 PowerShell 中）：
#   ./scripts/gpu5080/prepare-longcat.ps1 -ComfyRoot "D:\ComfyUI"
# 只做检查与打印指引，不自动下载大模型、不修改环境。逐项通过后按 43 号文档批 C 做 A/B 验收。
param(
  [string]$ComfyRoot = "",
  [string]$PythonExe = ""
)
$ErrorActionPreference = "Continue"
$fail = 0
function Check($name, $ok, $detail) {
  if ($ok) { Write-Host "[PASS] $name" -ForegroundColor Green }
  else { Write-Host "[FAIL] $name — $detail" -ForegroundColor Red; $script:fail++ }
}

Write-Host "=== 1. GPU 与驱动（Blackwell 须驱动 >= 572） ==="
$smi = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>$null
if ($smi) {
  Write-Host $smi
  $driver = [version](($smi -split ',')[1].Trim())
  Check "驱动 >= 572" ($driver -ge [version]"572.0") "升级 NVIDIA 驱动到 572+ 后重启"
  Check "识别到 RTX 5080（16GB）" ($smi -match "5080") "确认本机为目标 5080 机器"
} else { Check "nvidia-smi 可用" $false "安装/修复 NVIDIA 驱动" }

Write-Host "`n=== 2. PyTorch（须 >= 2.7 且 cu128+，含 sm_120） ==="
if (-not $PythonExe) {
  if ($ComfyRoot -and (Test-Path "$ComfyRoot\python_embeded\python.exe")) { $PythonExe = "$ComfyRoot\python_embeded\python.exe" }
  elseif ($ComfyRoot -and (Test-Path "$ComfyRoot\venv\Scripts\python.exe")) { $PythonExe = "$ComfyRoot\venv\Scripts\python.exe" }
  else { $PythonExe = "python" }
}
$torchInfo = & $PythonExe -c "import torch;print(torch.__version__);print(torch.version.cuda);print(torch.cuda.is_available());print(','.join(torch.cuda.get_arch_list()))" 2>$null
if ($torchInfo) {
  $ver, $cuda, $avail, $archs = $torchInfo
  Write-Host "torch=$ver cuda=$cuda available=$avail archs=$archs"
  Check "CUDA 可用" ($avail -eq "True") "pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128"
  Check "cu128 或更高" ($cuda -and [version]$cuda -ge [version]"12.8") "重装 cu128 构建（cu121/cu124 不支持 sm_120）"
  Check "含 sm_120" ($archs -match "sm_120") "重装 cu128 构建"
} else { Check "PyTorch 可导入" $false "用 -PythonExe 指定 ComfyUI 的 python" }

Write-Host "`n=== 3. ComfyUI 与 WanVideoWrapper ==="
if ($ComfyRoot) {
  Check "ComfyUI 目录存在" (Test-Path "$ComfyRoot\main.py") "用 -ComfyRoot 指定 ComfyUI 根目录"
  $wrapper = "$ComfyRoot\custom_nodes\ComfyUI-WanVideoWrapper"
  Check "WanVideoWrapper 已安装" (Test-Path $wrapper) "git clone https://github.com/kijai/ComfyUI-WanVideoWrapper 到 custom_nodes 并 pip install -r requirements.txt"
  if (Test-Path $wrapper) {
    Check "LongCat 节点存在（v1.5 Whisper）" (Test-Path "$wrapper\LongCat\nodes.py") "git -C $wrapper pull 更新到含 LongCat Avatar 1.5 的版本"
    Check "WhisperModelLoader 存在" (Test-Path "$wrapper\HuMo\nodes.py") "更新 WanVideoWrapper"
  }
  Write-Host "`n=== 4. 模型文件清单（放入 ComfyUI/models 对应目录） ==="
  $models = @(
    @{ Path = "$ComfyRoot\models\diffusion_models\LongCat\LongCat-Avatar-1.5-Q5_K_M.gguf"; Name = "LongCat 主模型 GGUF Q5_K_M（16GB 显存推荐；备选 Q4_K_M）"; From = "https://hf-mirror.com/vantagewithai/LongCat-Video-Avatar-1.5-GGUF-ComfyUI" },
    @{ Path = "$ComfyRoot\models\loras\LongCat_distill_lora_rank128_bf16.safetensors"; Name = "蒸馏 LoRA rank128（Avatar-1.5 专用，勿用旧 alpha64）"; From = "https://hf-mirror.com/Kijai/LongCat-Video_comfy" },
    @{ Path = "$ComfyRoot\models\audio_encoders\whisper_large_v3_fp16.safetensors"; Name = "Whisper-Large-v3 音频编码器"; From = "https://hf-mirror.com/Kijai/LongCat-Video_comfy" },
    @{ Path = "$ComfyRoot\models\vae\Wan2_1_VAE_bf16.safetensors"; Name = "Wan2.1 VAE"; From = "https://hf-mirror.com/Kijai/WanVideo_comfy" },
    @{ Path = "$ComfyRoot\models\text_encoders\umt5-xxl-enc-bf16.safetensors"; Name = "UMT5-XXL 文本编码器"; From = "https://hf-mirror.com/Kijai/WanVideo_comfy" }
  )
  foreach ($m in $models) { Check $m.Name (Test-Path $m.Path) ("下载: " + $m.From + " → " + $m.Path) }
} else { Write-Host "未指定 -ComfyRoot，跳过 ComfyUI/模型检查" -ForegroundColor Yellow }

Write-Host "`n=== 5. 注意力后端提醒 ==="
Write-Host "Blackwell 不支持 xformers；WanVideoWrapper 用 sdpa（模板默认）或 SageAttention。"
Write-Host "SageAttention 须 sm_120 预编译 wheel；用 KJNodes 'Patch Sage Attention' 节点 + sageattn_qk_int8_pv_fp16_cuda 后端，"
Write-Host "不要用 --use-sage-attention 全局 Triton 后端（Wan 系会黑屏）。sageattention 1.0.6 与 LongCat 不兼容（官方示例注记）。"

Write-Host "`n=== 6. 应用侧配置 ==="
Write-Host "服务设置页：视频提供方=comfyui，标准模式引擎=longcat，主模型/LoRA 文件名与上面一致；或环境变量："
Write-Host '  $env:DUOCAST_VIDEO_PROVIDER="comfyui"; $env:DUOCAST_VIDEO_ENGINE="longcat"'
Write-Host "配置保存后重启服务生效。"

Write-Host ""
if ($fail -eq 0) { Write-Host "全部通过。下一步：43 号文档批 C —— 同脚本同配音 MultiTalk vs LongCat A/B 样片验收（口型/聆听者/长片漂移/耗时/显存峰值）。" -ForegroundColor Green }
else { Write-Host "$fail 项未通过，按上面指引修复后重跑本脚本。" -ForegroundColor Yellow }
exit $fail