# HANDOFF · 双声播客工坊 DuoCast

> 交接文档。写给下一位接手这个仓库的开发者：读完这份文档，你应当能在本机把项目跑起来、看懂代码落在哪、知道哪些是真的、哪些还是占位、以及下一步从哪里开工。
>
- 交接日期：2026-09-10
- 当前迭代：mock 适配器联调版（v0.1）
- 权威需求文档：[04-详细优化方案.md](04-详细优化方案.md)

---

## 一句话现状

产品五阶段流程（创建本期 → 编辑对话 → 试听配音 → 预览画面 → 生成导出）的**前后端骨架已经全部打通并可运行**：九屏页面、任务状态机、事件推送、版本存储、确认点都在真实工作；但四个内容提供方（文本 / 图片 / 语音 / 视频）目前是 **mock 适配器**，返回占位数据，**还不能产出真实的脚本、图片、音频和成片**。

换句话说：这是一套「流程和编排是真的、内容生成是假的」的可演示骨架。

---

## 仓库结构

```
duo-podcast/
├── 01-页面详细设计文档.md      # 页面级设计（布局 / 元素 / 交互 / 数据模型 / 接口契约）
├── 02-项目实施方案文档.md      # 架构、显存预算、关键算法、任务恢复、路线图
├── 04-详细优化方案.md          # ★ 当前权威需求（v3.0 五阶段）
├── 05-UI设计系统与页面规划.html # 设计令牌、组件规范、前端落地映射
├── 06-后端设计规划.md          # 后端模块职责、API / 任务 / 存储 / 适配器落位
├── hf/                         # 九屏高保真设计稿（HF-00 ~ HF-08，静态 HTML）
├── apps/
│   ├── server/                 # 后端 FastAPI 服务
│   ├── web/                    # 前端 React + Vite
│   └── storage/                # 运行时数据（gitignored，不入库）
└── HANDOFF.md                  # 本文件
```

文档的单一权威原则：同一条规则（时长口径、双音轨、失效矩阵、确认策略）只在 04 及其指向的 01/02 章节定义一次，其他文档引用，不要在代码注释里另立口径。

---

## 本地运行

环境：Windows、Python ≥ 3.10、Node + pnpm。

```bash
# 后端（端口 8100）
cd apps/server
pip install -e .          # 或 pip install fastapi "uvicorn[standard]" pydantic sse-starlette
python -m uvicorn duocast.main:app --host 127.0.0.1 --port 8100

# 前端开发模式（端口 5178，/api 代理到 8100，支持热更新）
cd apps/web
pnpm install
pnpm dev

# 前端构建（产物 dist/ 由后端在 8100 直接托管，日常使用不必跑 Vite）
pnpm build
```

- 开发调试开 `http://127.0.0.1:5178`
- 构建后整站访问 `http://127.0.0.1:8100`
- 健康检查：`GET http://127.0.0.1:8100/api/health`

---

## 代码地图

### 后端 `apps/server/duocast/`

| 目录 | 文件 | 职责 |
|---|---|---|
| `core/` | `config.py` | 仓库根、端口、存储根、队列上限（端口可用 `DUOCAST_PORT` 覆盖） |
| | `eventbus.py` | 进程内事件总线，事件带序号、落 `events.jsonl`，SSE 推送 |
| | `logging.py` | 结构化 JSON 日志 |
| `domain/` | `project.py` `audio.py` `visual.py` `render.py` `job.py` | Pydantic 领域模型（工程、话轮、音频时间轨、画面变体、输出版本、任务） |
| `storage/` | `project_store.py` | 工程读写，revision 乐观锁（已用版本不被覆盖）、防抖落盘 |
| | `artifacts.py` | 不可变资产登记（manifest，哈希驱动复用） |
| | `machine.py` | 机器设置（提供方配置引用、显存分配） |
| `jobs/` | `manager.py` | 任务状态机 queued→running→succeeded/failed/cancelled；幂等 clientToken；GPU/API/CPU 三队列并发上限；暂停/取消 |
| | `recovery.py` | 启动时扫描中断任务并恢复 |
| `adapters/` | `base.py` | 四个提供方 Protocol + `CapabilityRegistry`（能力门控唯一数据源） |
| | `mock.py` | **当前在用的占位实现**（见下节） |
| `services/` | `script_svc.py` `voice_svc.py` `visual_svc.py` `render_svc.py` `stage_svc.py` | 业务编排：构造脚本版本、合成音频时间轨、画面变体、输出版本、阶段状态推导 |
| `api/` | `projects.py` `script.py` `voice.py` `visual.py` `render.py` `jobs.py` `events.py` `artifacts.py` `machine.py` `providers.py` | FastAPI 路由，前缀均为 `/api` |
| `main.py` | | 装配入口：存储 / 事件总线 / 任务管理器 / 适配器 / 路由；挂载前端 `dist`；启动自检 |

任务执行分派在 `main.py` 的 `_job_runner()` 内，按 `job.kind`（`script.generate` / `script.rewrite` / `tts.synthesize` / `visual.generate` / `renders`）路由到对应 service，完成后写回工程并登记资产。

### 前端 `apps/web/src/`

| 路径 | 职责 |
|---|---|
| `App.tsx` | 视图路由（五阶段 + 资产库/服务设置/任务中心）、SSE 接线、能力快照同步 |
| `components/wu.tsx` | workspace-ui 基线组件库（Button/Card/Dialog/Badge/Input/Modal 等，`wu-` 命名空间） |
| `components/AppShell.tsx` | 应用壳：左侧阶段导航 + 主区 + 检查器 + 底部 FootBar |
| `lib/api.ts` | API 客户端 + TanStack Query hooks（查询/变更） |
| `lib/sse.ts` | EventSource 封装，断线重连、`system.resync` 全量回读 |
| `lib/types.ts` | 与后端对齐的 TypeScript 类型 |
| `stores/index.ts` | Zustand：projectStore（当前工程/视图）、jobStore、serviceStore（能力门控） |
| `styles/tokens.css` `components.css` `duo-shell.css` | 设计令牌（暖白画布、深灰主色、品牌橙）、组件样式、布局 |
| `pages/` | 九屏：HomePage、CreateEpisodePage、EditScriptPage、VoicePage、VisualPage、RenderPage、AssetsPage、ServicesPage、TasksPage |

---

## mock 适配器：现在是假的部分

`apps/server/duocast/adapters/mock.py` 提供四个占位实现，`main.py` 启动时全部装载。它们的行为刻意对齐了契约（返回结构正确、带延迟以驱动任务事件），但内容是伪造的：

| 适配器 | 现在返回什么 | 真实实现应该做什么 |
|---|---|---|
| `MockTextProvider` | 按段落机械轮流分给 A/B；改写只做「但是→不过」等字符串替换 | 调文本 API，两次生成（内容结构 → 双人对话），返回结构化话轮 |
| `MockImageProvider` | 返回 `{placeholder: true}`，无真实图片 | 调图片 API 文生图/编辑，下载校验，登记真实图片资产 |
| `MockTTSProvider` | 只按字数估算 `sampleCount`（220ms/字，48kHz），**无音频文件** | 本地 TTS 引擎或 MiniMax，产出真实 PCM/音频与时间戳 |
| `MockVideoProvider` | 返回假任务 id 字符串 | 提交 ComfyUI 双人口型工作流，产出视频片段 |

能力快照 `GET /api/providers/capabilities` 当前返回的是 mock 声明的能力（前端据此做门控显示），换成真实适配器后会反映真实能力。前端「服务设置」页的密钥存储（keyring）也是占位，标注为「关口 B 接入」。

---

## 已完成与已验证

后端：
- 工程存储 + revision 乐观锁 + 防抖落盘；不可变资产登记；机器设置
- 任务状态机（幂等 clientToken、三队列、暂停/取消、启动恢复扫描）
- 五条任务链路：脚本生成、候选改写（不落盘，用户接受后才替换）、配音合成、画面生成、渲染导出
- 工程域含音频时间轨（48kHz 母轨、整数样本偏移、单元偏移表）、画面变体、输出版本、三个确认点 approvals
- SSE 事件流 + 前端实时推送

前端：
- 九屏全部接入真实数据流（TanStack Query + SSE），`tsc --noEmit && vite build` 零错误
- HF-02 话轮编辑/选区改写/质量提示、HF-03 声音绑定/合成/时间轨、HF-04 变体/样片确认、HF-05 分段计划/渲染、HF-08 任务暂停/取消/详情均已联通后端

端到端验证：`apps/server/smoke_e2e.py` 走通 建工程 → 脚本生成/确认 → 配音合成/确认 → 画面生成/样片确认 → 渲染导出 → 资产登记 → 机器设置 → 提供方测试 全链路。运行：

```bash
cd apps/server
python smoke_e2e.py
```

---

## 下一步开工顺序

按 02 的交付关口制，骨架已具备，接下来进入「让内容生成变真」的阶段：

1. **真实提供方适配器**（最高优先）。契约在 `adapters/base.py`，新增实现后在 `main.py` 替换对应 mock。建议顺序：文本 API → 图片 API → 本地 TTS / MiniMax → ComfyUI 视频。凭据存储接 keyring（关口 B）。
2. **关口 A 硬件实测**（视频侧的硬门槛）。验证 InfiniteTalk/MultiTalk 双轨输入适配（官方 `add` 是串行拼接，不能直接当双轨相加）、16GB 显存跑 480p 双人的可行性、跨段边界连续性。这一项不通过，视频页不进入完全实现。
3. **前端可视化库接入**：WaveSurfer（时间轨波形/试听）、Konva（人物区域标注）、dnd-kit（话轮拖拽排序）。目前时间轨是 CSS 条形占位。
4. **读音词典服务端持久化**；单元/集成测试补全（现有 `tests/` 仅冒烟）。
5. 真实音频/图片产物的文件落盘与下载路径（当前资产登记只写元数据，无实体文件）。

---

## 已知坑与约定

- **仓库根路径**：`core/config.py` 用 `Path(__file__).resolve().parents[4]` 定位仓库根（文件在 `apps/server/duocast/core/` 下，向上 4 层才是仓库根）。曾误写成 `parents[3]` 导致定位到 `apps/`，静态资源挂载到不存在的 `apps/apps/web/dist`，表现为 8100 只返回「前端未构建」占位 JSON。改动这块路径后务必重启后端并验证 `GET /` 返回的是 HTML 而非 JSON。
- **运行时数据不入库**：`apps/storage/`（工程、任务、资产、事件、机器设置）已在 `.gitignore`，换机器或重置时删除该目录即可回到空状态。
- **存储根**：默认 `apps/storage`，可用环境变量 `DUOCAST_STORAGE` 覆盖。
- **端口**：后端 8100（`DUOCAST_PORT` 可改），前端 dev 5178（vite 固定 strictPort）。
- **时长权威**：节目总时长以最终 48kHz PCM 母轨样本数为准，不是最后一句时间码；改 TTS 适配器时必须返回真实 `sampleCount`。
- **mock 与真实不要混用口径**：替换适配器时保持 `capabilities` 字段与实际能力一致，前端门控依赖它。

---

## 常用命令速查

```bash
# 重启后端（改了 Python 代码后）
# 停掉 8100 进程后：
cd apps/server && python -m uvicorn duocast.main:app --host 127.0.0.1 --port 8100

# 前端类型检查 + 构建
cd apps/web && pnpm build

# 端到端冒烟
cd apps/server && python smoke_e2e.py
```
