# 隔离专家链路实验工具

目标与质量门槛见[优化方案](../../docs/motion-workflow/OPTIMIZATION_PLAN.md)。本工具是独立原始工作流实验，不开放正式专家产品入口，不写工程的人工质量采用。

## 输入与默认行为

准备本机JSON配置，必须包含以下四个绝对路径；注意力实验可另外显式提供`attentionSite`绝对路径：

```json
{
  "sourcePlan": "C:/DuoCast/review/expert-plan.json",
  "enginePython": "C:/DuoCast/engine/venv/Scripts/python.exe",
  "comfyRoot": "C:/DuoCast/engine/ComfyUI",
  "outputRoot": "C:/DuoCast/review/optimization-runs"
}
```

`sourcePlan`是已准备的专家窗口清单：每项有原始workflow文件、modelFrames/coreFrames、trim区间，以及每个输入文件的path/fileHash。沿用现有`prepare_expert_motion.py`和已登记素材建立，不通过HTTP请求传入任意路径。

默认`plan`只读取本机文件并验证摘要，不发HTTP、不启动引擎、不调用模型：

```powershell
python scripts/acceptance/benchmark_expert.py --config .review/expert-optimization/config.json --run-id baseline-screen --indices 0 2
```

明确运行才加`--stage run`。每个run-id是不可变的一次实验；不要修改已有清单或用同一ID重新提交。

## 可选变体

| variant | CUDA_LAUNCH_BLOCKING | blocks_to_swap |
|---|---|---|
| baseline | 1 | 38 |
| async | 0 | 38 |
| baseline-swap36 / baseline-swap32 | 1 | 36 / 32 |
| async-swap36 / async-swap32 | 0 | 36 / 32 |
| async-nonblocking | 0 | 38；仅打开use_non_blocking |
| async-prefetch1 | 0 | 38；仅打开prefetch_blocks=1 |
| async-sage | 0 | 38；仅将模型attention_mode由sdpa改为sageattn |

只选择有显存余量的变体。当前实验默认在物理GPU0运行，使用隔离8192端口（可选8192～8199），进程环境独立。运行前暂停已知闲置实验引擎并确认GPU没有其他推理任务；基准锁只协调本工具，不能替代正式应用GPU队列。

```powershell
python scripts/acceptance/benchmark_expert.py --config .review/expert-optimization/config.json --variant baseline --run-id a0-profile --indices 0 2 --profile-nodes --stage run
```

启动身份端点验证随机launchToken及实际同步配置。PyTorch版本、CUDA版本和设备名随实际进程记录。CPU不可变权重保护及原兼容开关保留；不覆盖共享模型或修改共享第三方源码。原`start_guarded.py`默认仍为8191端口、同步值1，仅显式实验环境改变行为。

Sage候选必须显式设置`attentionSite`，其中只使用`sageattention/`包。可将本机已兼容包复制到独立实验目录；不向共享环境安装或升级依赖。配置阶段记录包内Python和本机扩展文件摘要，启动前与引擎导入时再次检查。只导入指定包，不将整个外部site-packages加入PYTHONPATH；默认正式启动没有该环境变量，不加载此候选。

公平对照时SDPA与Sage使用相同`attentionSite`、探针和代码，仅修改模型后端。身份端点提供实际包位置、treeHash及成功调用计数；每个Sage窗口要求调用计数增长，否则拒绝将静默回退当作成功。算子级随机输入的速度/误差不能证明视频质量或整片速度。Sage改变数值结果，候选仍需完整同音轨视频对照及人工质量确认。

`--profile-nodes`安装节点边界同步计时，输出`engine/node-timings.jsonl`；该同步会影响性能，profile与非profile不能直接比较。节点峰值为自引擎启动以来的PyTorch分配/保留峰值；nvidia-smi是整卡离散采样，不能宣称捕获全部瞬时峰值。

## 资源保护与退出

- nvidia-smi不可观测或读数无效时不启动模型实验。启动前整卡至少需要12288MiB可用显存，这是当前16GB实验的保守资源筛选值，不是模型通用最低显存声明；未通过时保留`GPUStartupPreflight`记录且不启动引擎。运行中整卡余量低于1536MiB则终止该批。
- 单次NVML查询失败可立即重试一次，每次上限5秒；连续失败仍停止，不无限放宽监测。有效的低显存读数直接停止，不重试掩盖。该保护只约束显存，不能代表主机内存足够；窗口异常变慢时需另查系统内存压力。
- 子进程使用既有OwnedProcess进程树管理，正常结束、异常和取消都清理引擎及后代；强杀父进程时Windows JobObject关闭负责清理。
- 每次POST前先保存intent、完整工作流摘要和引擎身份。响应丢失不重发；保留receipt以供人工核对。
- 已知未完成批次不自动重新启动模型：引擎进程可能已被清理，其RAM历史不保证跨重启保留。应先查原ID、输出和停止状态，再使用新的显式批次处理；不要把此限制说成已经完成远端恢复能力。
- 成功批次再次运行同配置只核验已有媒体摘要；输入、配置或代码身份改变会拒绝复用。严禁把缓存的sampler输出计为一次实际推理。
- 结果包含startupSeconds、各段引擎执行时间、原始媒体、完整解码和显存采样；不自动批准口型、身份、融合或两分钟性能。

## 报告与比较

```powershell
python scripts/acceptance/report_expert_benchmark.py .review/expert-optimization/runs/a0-profile/state.json --candidate .review/expert-optimization/runs/a1-profile/state.json --out .review/expert-optimization/comparison.json
```

报告拒绝比较源码、输入、窗口选择或计时探针不同的批次。它检查媒体哈希，分别计算解码画面和音轨摘要。逐帧相同只证明这些固定窗口的输出相同，不代表任意新内容均通过；单次配对速度不代表统计结论。资源余量未过门槛的候选不能因稍快而入选。

专项回归：

```powershell
python -m pytest apps/server/tests/test_expert_benchmark.py apps/server/tests/test_owned_process.py -q
```

## 原像素区域实验和长片边界

`prepare_expert_roi.py`只做CPU准备：由已确认逐帧人脸坐标推导联合区域（40%上下文边距、16像素对齐），裁剪锚点、参考视频和语义遮罩，保持脸部像素尺度与原音轨。参考视频的原静音轨必须保留，VHS加载节点会读取它。当前只支持同框两角色，不是所有机位的正式功能。

`compose_expert_roi.py`核对来源摘要、连续窗口、上下文裁剪、原PCM条件与帧数，在完整原动作参考上按人脸轨迹柔边回贴。编码前区域外像素保持；有损编码后不声称RGB完全相同。ROI不能交给一般全画面拼接器直接拉伸。相关角色身份、口型与边缘融合必须另行人工采用。

长片仍须显式运行：默认最多5窗口；`--timeline-limit 16`允许最多30秒/16窗口，`--timeline-limit 50`允许最多120秒/50窗口。长片源清单须有整数sampleRate/sampleCount；超过声明时长拒绝。整轮引擎上限分别1800/4200秒，并保留原任务ID。原始81模型帧/64核心帧限制没有放宽。主机可用内存随采样记录，无法获取时为unknown。

纯音频参考不要传给旧的双视频`compare_audio`检查器。`verify_master_audio.py`会从标准PCM母轨独立编码AAC/192k，再严格核对交付片音频流与参考的解码PCM摘要；它支持没有视频流的音频参考。该检查证明音轨一致，不代替可见口型同步判断。

```powershell
python scripts/acceptance/verify_master_audio.py --video C:/DuoCast/output.mp4 --master C:/DuoCast/master.wav --work C:/DuoCast/new-audio-verification
python -m pytest apps/server/tests/test_expert_benchmark.py apps/server/tests/test_expert_benchmark_assembly.py apps/server/tests/test_expert_roi.py apps/server/tests/test_master_audio_verification.py apps/server/tests/test_owned_process.py -q
```
