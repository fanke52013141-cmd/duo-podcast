# 快速模式运行时

本目录跟踪自有工作进程、素材准备和部署工具。模型权重、第三方完整源码、Python 环境、参考音频和成片留在本机，不提交 Git。

## 这次版本

`deferred-scale-v2`：在导出所需的画布上融合 256 像素口型输出，避免先缩回小脸再整幅放大；保留源素材长宽比例，并避免把已是高清的素材先降采样。监听帧沿用原闭口动作库。原有模式和旧工程默认行为不变。

界面新建节目可选快速模式，生成导出可选原始 / 清晰 / 更清晰三个编码增强档位。增强只是温和锐化和降低压缩，不是 AI 超分。GFPGAN、时序修复仅在独立实验中测试，没有进入生产依赖或默认处理链。

## 外置运行时布局

服务端设置 `DUOCAST_FAST_ENGINE` 指向运行目录，例如 `C:\DuoCast\fast-runtime`。正式启动器 `scripts/digital-human/start.ps1` 也支持本机 `runtime.local.json` 中的可选 `fastEngineRoot` 字段。

```text
fast-runtime/
  worker.py, render_geometry.py, quality.py, runtime_version.json
  venv/Scripts/python.exe
  vendor/MuseTalk/
  models/musetalkV15/{musetalk.json,unet.pth}
  models/sd-vae/{config.json,diffusion_pytorch_model.bin}
  models/whisper/{config.json,pytorch_model.bin,preprocessor_config.json}
  assets/{stage.png,template_A.mp4,template_B.mp4,manifest.json,face_coordinates.json,parsing_bisenet.pth}
  cache/
```

测试依赖为 Python 3.12、PyTorch 2.8.0+cu128、torchvision 0.23、torchaudio 2.8、diffusers 0.35、transformers 4.57.1、OpenCV、NumPy、Pillow、librosa、accelerate，以及可执行的 FFmpeg / ffprobe。服务端继续使用轻量环境，GPU 工作进程单独启动。可复用本机兼容运行库，但不能假定另一台机器已有相同盘符或共享环境。

第三方 MuseTalk 源码测试版本：`TMElyralab/MuseTalk @ 0a89dec45a0192b824e3cf4daf96c239440c5ed8`。模型来源及其许可证需分别遵守官方项目要求；研究示例不作为生产素材使用。

配置外置模型、环境、素材后，更新工作进程源码：

```powershell
python scripts/fast-mode/install_runtime.py --runtime C:\DuoCast\fast-runtime
python scripts/fast-mode/validate_template.py --runtime C:\DuoCast\fast-runtime
$env:DUOCAST_FAST_ENGINE='C:\DuoCast\fast-runtime'
```

`install_runtime.py` 只复制本仓库工作进程和策略文件，不自动下载权重、不覆盖模型或素材。更新前请确认没有运行中的 GPU 任务。模型仍由使用者从官方来源准备，工具不会安装任意来源的软件。

## 准备高清动作素材

建议使用实际生成或拍摄的 720p 及以上双人同框视频。1080p 导出画布不能替代高清输入；人脸像素尺寸、转头角度和原生口型模型仍会限制细节。新版代码支持高清模板，但本次没有把一秒高清实验升级为正式动作库。

准备一段经过审看、无预热畸变的双人横屏动作视频，A 在左、B 在右；视频中两人都需要有说话和闭口倾听的区间。源视频的说话时间元数据采用：

```json
{"turns":[{"speaker":"A","start":0,"end":2},{"speaker":"B","start":2.32,"end":4.32}]}
```

时间以输入视频开头为零点、秒为单位，应来自生成或剪辑时间线，而不是让工具猜测真实口型。生成器有预热帧时先按生成器的统一规则裁掉，再同步时间线。不要按个别人脸手工挑选或补坏帧。

使用 [OpenCV 官方 YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) 检测权重，自动分左右轨、检测人脸并计算坐标：

```powershell
python scripts/fast-mode/prepare_template.py --video approved720.mp4 --stage stage.png --timeline source_turns.json --detector face_detection_yunet_2023mar.onnx --output C:\DuoCast\templates\studio720
python scripts/fast-mode/validate_template.py --runtime C:\DuoCast\templates\studio720
```

输出目录必须为空，不会覆盖正在使用的运行目录。默认拒绝低于 720p 的新素材；`--allow-low-resolution` 只用于已有低清素材兼容或测试。检测不到可靠人脸、坐标越界、两轨尺寸/帧数不一致、缺少发言或倾听区间时失败，不会把坏素材默认为可用。

新素材需观看验证人物一致性、手部、转头与闭口状态，再在无任务运行时备份并更新外置运行时的 `assets/`。潜变量缓存以源视频内容、坐标和 VAE 安装信息计算键，替换素材后重新准备。一次只有一套活动演播室模板；多模板库尚未实现，更换整套人物需保留旧运行目录以便旧工程重渲染。

## 已测及限制

- 旧模式兼容、快速模式创建、输入哈希、非法画质选项、几何映射、缓存失效及运行时安装均有后端回归测试。
- 一分钟真实样片和四角色小样本对照已完成；小样本不能证明对所有人物都提高质量。
- 高清探索是现有 480p 训练底模在更高分辨率采样：1280×720、33 帧，统一去预热后仅一秒有效素材；不是原生高清底模或长片稳定性测试。
- 新版需要更新外置 worker；只更新服务端而不部署运行时，不能得到新的融合效果。
- 清理 `cache/` 会重建潜变量，不会删除源素材；不要在 GPU 任务运行中清理。旧 `latents_v1_*.pt` 已不再使用，可在停机时删除。
