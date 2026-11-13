# HANDOFF · 双声播客工坊 DuoCast

更新时间：2026-11-11。本页记录当前代码状态；历史细节见 [历史交接记录](HANDOFF-HISTORY.md)。

## 当前代码基线

- 远程主线 `origin/main`：`105c7f64`（2026-10-26）。
- 动作工作流远程基线：`545dbf00`（2026-10-27），领先主线15个提交，尚未合入主线。
- 当前开发分支：`codex/cpu-workflow-optimization`，基于动作工作流分支；2026-11-11 批已提交并推送。提交号以该分支Git日志为准。
- 本轮范围与验证：[无显卡开发交付与验证](39-无显卡开发交付与验证.md)。上轮两个交互修复见 [38记录](38-无显卡交互优化记录.md)。
- 后续审查修复与可重复的12项浏览器回归见 [40记录](40-代码审查与浏览器自动回归.md)。

## 2026-11-11 批 · 用户视角纠偏优化与外部上传对接（41/42号方案落地）

诊断与方案见 [41-用户视角流程审查](41-用户视角流程审查与人机纠偏优化.md)、[42-全流程外部上传对接与纠偏优化方案](42-全流程外部上传对接与纠偏优化方案.md)（42号含逐项落地状态）。本批已提交并推送到 `origin`（fanke52013141-cmd/duo-podcast）。

- **批1 信任与重试（P0）**：①脚本生成返回 `applied` 标记修复假成功；`POST /api/jobs/{id}/retry` 继承已完成单元账本只补失败段；TTS 中断重启带账本续跑；⑤分段网格读真实复用计划（依赖哈希+文件完整性）。
- **批2 上传通道**：脚本 JSON/txt、单元成品音频、图片+区域 JSON、整片视频四类导入，一律注入候选待人显式采用；统一 Ingest API `/api/ingest/*`（信封校验、clientToken 幂等、expectedRevision 乐观锁、JSON Schema 自描述、`DUOCAST_API_TOKEN` Bearer 门禁，默认仅本机）。
- **批3 纠偏解锁（P1）**：主动撤销确认（②③④，保留审计）、音频时间轨历史回退、自定义试听文本；音色下拉已按用户要求移除（音色由参考音频决定）。
- **批4 体验（P2）**：⑤过期横幅+一键重生成、交付检查直达、任意话轮拆分、读音词条原地编辑、机位锁定持久化（新增 `cameraLocks` 工程字段）、接受样片只校验视觉哈希（旧快照兼容回落）、A→B→A 一键建议、影响预告动态化、超限引导。
- **P1-C 快速模式解锁**：换角色/场景/底图不再 422 拒绝，改为 `motion_swap` 派生新动作包再改参考图（原包与其他工程不受影响），新包不就绪→现有渲染门槛自动拦截→引导重新准备；无动作包的快速模板工程明确拒绝。CPU 回归验证派生/失效/拦截语义。
- **验证**：后端 **989 passed / 18 skipped**（新增 55 项回归，5 个测试文件）；前端 34 项 + tsc + 生产构建通过。
- **仍待 GPU（RTX 5080）**：快速模式局部重做（执行侧无 reuse/redo 分支，未实现）；换图后真实 prepare→inspect→review 与画面效果验收。二期高级项（整期时间轴导入、分段导入、Webhook/外部回调）未做。

## 2026-11-11 增补批 · 创建时选分辨率 + 近景虚线框（42号第七/八部分）

- **创建时选定导出分辨率**：新建弹窗选「原有模式（标准）」时可选 480p/720p/1080p（附耗时提示）；`POST /api/projects` 接受 `renderSpec` 并落盘，创建与 PATCH 共用 `_validate_render_spec` 校验；⑤渲染页读 `renderSpec` 初始化，渲染透传链路零改动。快速模式不显示该选项（输出由动作素材原生分辨率 + fastQuality 决定）。
- **近景虚线框**：`VisualVariant` 新增 `closeupRegions`（归一化，空=自动半身推导，旧工程零变化）；`video_plans.closeup_box` 显式框优先、完全按框裁切，标准（ComfyUI）与快速模板（MuseTalk）两处切镜调用点统一接入；`POST /visual/regions` 增可选 `closeupRegions`（`{}`=恢复自动，不带字段不清空已存框）；④「校准人物区域」升级为实线人物框 + 虚线近景框（拖动/数字精调/一键恢复自动/A-B 同步交换）。改近景框自动使样片确认与分段缓存失效（依赖哈希已含画面变体）。动作包快速模式近景来自包内机位，不在此列。
- **验证**：后端 **1014 passed / 18 skipped**（新增 13 项分辨率 + 12 项近景框回归）；前端 34 项 + tsc + 生产构建通过。
- **边界**：近景框为静态画布坐标（不做人脸追踪），框过小放大会损失细节；真实 GPU 切镜效果验收仍留 RTX 5080。

## 2026-11-12 · LongCat 引擎评估后搁置（用户决策）

- 曾接入 LongCat-Avatar-1.5 可选引擎（提交 `7b1f094`），经社区实证评估后**用户拍板搁置，已 revert 删除**：标准模式继续以 MultiTalk/InfiniteTalk 为唯一引擎。
- 依据：坐姿双人播客是 InfiniteTalk 主场（LongCat 强项是全身表演，用不上）；消费级硬件速度无收益（2 分钟约 40–90 分钟 vs 现约 2 小时）；5080 无社区一手实测；质量优势无独立头对头证明。
- 评估过程与实现细节留存 git 历史提交 `7b1f094`（含 43 号评估文档），未来需要时可查阅或 cherry-pick 恢复；当前代码库不含任何 LongCat 相关代码与文档。

## 2026-11-12 批 · 标准模式加速调优参数（44号，2026-11-13 已完成 5080 实测）

- **8 个加速参数可配置**（环境变量 `DUOCAST_VIDEO_*`，默认值=现网行为零变化）：块交换数/预取、注意力后端（sdpa→sageattn）、text/audio CFG、torch.compile 开关、LoRA 文件名/强度。落点：config.py、comfyui_video.py（`_apply_acceleration`+`acceleration_fingerprint`）、main.py 装配与 `activeConfig.video_acceleration` 可见。
- **缓存指纹含全部加速参数**：切配置自动重算对应片段，不误用旧配置产物；同配置回退命中旧缓存。
- **5080 实测胜出组合（29.1s 固定工程 EP9930）：`sageattn + text/audio CFG 1.0`，块交换/预取保持默认、compile 关。整片 79.1min→24.5min，3.23× 提速（省 69%），显存 15.0→13.8GB，口型/聆听者/手部/身份动态验证通过。** 已固化到 start.ps1 + 用户环境变量。详见 [44 号文档第五节](44-标准模式加速调优交接.md)。
- **与审计预期的差异**：块交换/预取在本机无收益（显存被 137 帧窗口激活值占满，降 swap 反而换页）；SageAttention 是最大收益（1.97×）；CFG 单前向再省 41%；torch.compile 因 inductor 循环导入不可用。SageAttention 2.1.1 须装在引擎实际用的 `InfiniteTalk_Runtime\venv`（非 `ComfyUI\python`），装后重启引擎。
- **梯级 A/B 补齐（2026-11-13）**：第 3 步 audio2 变体（text1/audio2）42.6min，口型/聆听者同 audio1 好但慢 1.74×→不选。第 5 步 2 分钟整期（EP9931，24 段）实测 **97.7min**，提速达标；5 分钟整期用户选择跳过（EP9932 工程已备未渲染）。
- **长片背景退化：发现→修复→复测闭环**：2 分钟整期（chain 修复前）曾背景逐段累积崩坏+身份漂移（Laplacian 首尾 +186%、色度碎点 47.8→206.5、色偏 22.6/255），根因是 47 号"上一段末帧当参考图"参考帧链接误差累积，与加速无关。**47 号修复（`4fa1ecd`，`DUOCAST_ANCHOR_MODE=clean` 每段重锚定干净底图）入库后已用 EP9931 clean 重跑复测（J89F205F5C7，98.7min）：quality_harness PASS（背景偏离趋势比 1.00）、Laplacian 首尾 +5.3%、色偏 2.9/255、身份锁定、口型帧级对齐——加速+背景修复叠加后长片质量达标。** 注意 `DUOCAST_ANCHOR_MODE` 代码默认 chain，须显式设 clean（start.ps1 已默认设 clean）；已纳入缓存指纹。本次加速 A/B 未改参考帧逻辑（守住与 47 号任务边界）。
- **验证**：后端 **1120 passed / 20 skipped**；前端 34 项 + tsc + 构建全绿。

## 2026-11-13 批 · 社区对标升级：SageAttention 2.2 + SeedVR2 超分（44号第十节）

- **社区调研结论**：InfiniteTalk 仍是开源长片/双人首选；AptAvatar（淘天 2 步 720p，论文 60×）/SoulX-FlashTalk/LiveAvatar 纸面更强但被显存墙挡住（80GB 起步或无消费级验证），列为观察项。本轮落地两个 16GB 可跑的确定收益项。
- **SageAttention 2.1.1→2.2.0.post3 已采纳**：woct0rdho wheel（cu128torch2.8.0，SHA256 校验），装引擎 venv，2.1.1 备份 `InfiniteTalk_Runtime\sageattention_backup_2.1.1\`。EP9930 同种子 A/B：**23.5min vs 24.5min（快 4%）**，显存 +0.5GB，质量等价或更好（t=20 的 2.1.1 马赛克伪影在 2.2 消失）。**运维坑：缓存指纹不含 sage 包版本，升级包后须手动作废对应 video_motion 缓存目录再重渲染**。
- **SeedVR2-3B FP8 导出超分（可选引擎）**：`DUOCAST_EXPORT_SR=1` + `DUOCAST_EXPORT_SR_ENGINE=seedvr2` + `DUOCAST_SEEDVR2_PYTHON=<引擎venv python>`；默认引擎仍 realesrgan 零变化。三方 A/B（同 5s 片段）：女脸 Laplacian 235 vs Real-ESRGAN 166，视觉核验皮肤毛孔/发丝/齿缝自然、无磨皮塑料感、无过锐白边、时序最稳；**代价 6.5min/5s → 2 分钟整片约 2.5h，定位精品定稿档**。16GB 必须 VAE 分块+DiT/VAE 互卸载（命令已内置）。依赖在 `tools/seedvr2/` + `tools/seedvr2_models/`（gitignore）。
- **代码落点**：`services/video_sr.py` 双引擎（sr_engine/find_seedvr2/seedvr2_command 纯函数 + _super_resolve_seedvr2）；`tests/test_video_sr.py` 39 项。**验证：后端全量 1159 passed / 20 skipped；模块级真实端到端 PASS（397s，1080p+音轨）**。详见 [44 号文档第十节](44-标准模式加速调优交接.md)。
- **转正与清理（同日，用户拍板）**：SageAttention 2.2 + SeedVR2 成为正式默认方案——超分默认启用（`DUOCAST_EXPORT_SR=0` 关闭回退 ffmpeg 放大；2 分钟整片导出约多花 2.5h，用户已知晓）；Real-ESRGAN 代码路径与 tools/realesrgan 已移除（回收站可恢复），旧 sage2.1.1 分段缓存备份已清；保留引擎 venv `sageattention_backup_2.1.1/` 作 sage 回退。start.ps1 + 用户环境变量固化，服务已重启验证。`video_sr.py` 精简为 SeedVR2 单引擎，`test_video_sr.py` 30 项，后端全量 **1150 passed / 20 skipped**。见 44 号 10.3。

## 已实现能力

五阶段制作、工程版本存储、任务队列、确认点、SSE、真实提供方适配、快速模式、交付检查与备份维护均已有代码。九月“全部提供方都是mock”的历史描述不代表当前实现。

动作分支包含固定版本动作包、素材检查、原片/处理后同步审看、独立准备任务、快速渲染输入、分层缓存及运行时管理。完整GPU效果验收仍未完成，任务账本见 [IMPLEMENTATION](docs/motion-workflow/IMPLEMENTATION.md) 和 [PROGRESS](docs/motion-workflow/PROGRESS.md)。

本轮新增或修复：

- 无显卡启动预检、独立端口与存储参数、明确标注的CPU测试素材工具。
- 脚本输入中离页、恢复保护、远程版本切换保护、替换预览失效处理。
- 弹窗与选项卡键盘验收，640/900像素布局和滚动修复。
- 真实文件波形、定位与选段试听；mock模式也能播放已有媒体。
- 备份携带固定动作包及引用媒体，迁移时另存新包并重置检查与人工采用。
- 原外部任务只读核对、已知动作准备续查入口；缺ID或响应未知不自动重采样。
- CPU片段依赖规划：比较实际PCM、源帧、坐标、机位与历史种子；候选不等于实际GPU复用。
- 逐帧滑杆、同步定位、动作草稿冲突保留、下载副本与另存新包。

## 无显卡电脑启动

最终验证（本批审查后）：后端 **934 passed / 18 skipped / 1 warning**；前端 **34项**、类型检查及生产构建通过；Chromium **12/12**、隔离场景 **12/12** 通过。18项为符号链接权限或真实模型/运行时依赖跳过，警告为既有Starlette/httpx弃用提示。锁文件冻结安装通过，浏览器回归结束后18327监听与测试进程已清理。

首次依赖安装、配置和备份说明见 [scripts/README](scripts/README.md)。依赖就绪后，在仓库根执行：

```powershell
./scripts/start.ps1 -Mode mock -CheckOnly -Port 18127
./scripts/start.ps1 -Mode mock -Port 18127 -StorageRoot .review/my-cpu-workspace
```

访问 `http://127.0.0.1:18127`。mock不会启动GPU引擎或付费生成；工程已有WAV/视频仍可审看。

需要三秒测试音、测试图案视频和未知任务记录时，先运行以下命令；目标目录必须全新或为空：

```powershell
apps/server/.venv/Scripts/python.exe scripts/acceptance/seed_cpu_workspace.py --storage .review/my-cpu-fixtures
./scripts/start.ps1 -Mode mock -Port 18128 -StorageRoot .review/my-cpu-fixtures
```

测试素材不代表真实语音、人物动作或质量通过。工具执行环境禁止本机监听时，应使用允许监听的终端；不要误诊为应用启动死锁。

## 接续与交付边界

1. 本批已审查并固化自动回归；更新代码后先运行40记录中的验证命令，再接入真实工程。
2. RTX 5080电脑固定同一提交，补真实引擎绑定、专家模式恢复执行、动作保持、GPU分段复用、长片和性能验收。
3. 只读核对不自动采用引擎结果；专家生成入口仍受既有门槛限制。CPU规划未验证运行时或渲染缓存输出。
4. 跨目录迁移、浏览器、PCM和FFmpeg测试已补；第二台物理机器的全新安装与真实服务验收仍须在该机器执行。
5. 配置或模板更新后，用独立存储先验证，再处理真实工程。

产品规则继续以 [04-详细优化方案](04-详细优化方案.md) 及其指向章节为准。UI壳、样本时钟和历史约定见历史交接。运行数据、模型、密钥和验收截图不入Git。
