# 可复现媒体验收工具

此工具只读检查输入，默认不调用模型、网络或付费服务。报告默认写入 Git 忽略的 `.review/motion-acceptance/`。不要把私有音频、模型或成片提交进 Git。

从仓库根执行（也可替换为本机 Python 路径）：

```powershell
apps/server/.venv/Scripts/python.exe scripts/acceptance/run_motion_acceptance.py --output .review/example-media.json media C:/media/candidate.mp4 --duration 8 --fps 25 --width 1280 --height 720
apps/server/.venv/Scripts/python.exe scripts/acceptance/run_motion_acceptance.py --output .review/example-audio.json compare-audio C:/media/two-shot.mp4 C:/media/reverse.mp4
apps/server/.venv/Scripts/python.exe scripts/acceptance/run_motion_acceptance.py --output .review/example-ledger.json manifest --evidence-report .review/example-media.json --evidence-report .review/example-audio.json
```

输出路径必须不存在，不允许覆盖输入。`media` 默认要求音频，动作原片可明确传 `--allow-silent`。它核验完整解码、实际读取的帧数、要求的规格和可取得的声画时长；不是人脸、动作或口型自然度验收。`compare-audio`先验证两份媒体，再计算相同 48kHz 单声道 PCM 格式的 SHA256；相同声音也不能证明画面或角色绑定正确。

`requirements.json`保留全部 T01–T12、V01–V18 以及待填写的证据槽。`manifest`只附加工具报告的输入哈希、事实和限制，不会因文件存在或某个命令成功把整个需求改成通过。原型演示、自动测试、人工审看和正式集成应分别提供证据。生成 ledger 命令返回成功仅表示记录成功，ledger 的 `passed` 仍为 false，需求保持待审/未运行。

## 显式性能测量

以下命令只演示参数；`benchmark`实际执行`--`后的 argv，不通过 shell，不提供默认生成命令。调用者自行选择已授权的本机程序；密钥只放环境变量，不放参数或内联代码。

```powershell
apps/server/.venv/Scripts/python.exe scripts/acceptance/run_motion_acceptance.py --output .review/timing.json --timeout 1800 benchmark --warm-runs 5 -- python path/to/local_worker.py path/to/request.json
```

冷启动测量必须提供 `--cold-runs` 和 `--reset-command-json`（重置程序 argv 的 JSON 数组），每轮先执行重置。重置失败会停止测量。冷态/热态为操作方声明，工具不能自动证明清理了哪些模型或缓存；模型常驻的热推理需要由被测程序另提供计时，不能用重复创建 Python 进程冒充。结果逐轮保存退出码、超时、耗时和输出哈希，按冷/热态统计中位和最慢值。命令输出正文默认不存入报告。

报告记录 Git HEAD、Python、FFmpeg/ffprobe 版本和命令。常见敏感参数及内联程序正文被隐藏并以摘要记录；不采集环境变量。命令通过不等于效果通过，性能优化必须与真实媒体和人工质量检查一起评估。

本目录脚本可由后端测试直接载入，测试用 FFmpeg 生成小型临时媒体，覆盖规格错误、音频不同、无音频、截断文件和无证据不得自动完成需求。
