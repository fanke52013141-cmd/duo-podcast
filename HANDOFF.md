# HANDOFF · 双声播客工坊 DuoCast

> **当前交接状态（2026-09-16）**：v3.1 修复批次已与远程 `806f654`（第二轮审查 P0×2/P1×6/P2×15 落地）完成 rebase 合并（合并提交 `a3177ce`），6 处冲突全部手工解决并保留双方有效修复。合并后测试契约差异（ID 中文/空格白名单、`id:null` 自动编号、PATCH 字段白名单）已对齐，后端 411 tests passed，前端 tsc 零错误 + vite 构建通过。此前记录见 [09-优化落地记录.md](09-优化落地记录.md) 与 [11-代码审查报告.md](11-代码审查报告.md)。详见下文「v3.1 修复批次」与「与远程 806f654 合并（2026-09-16）」。

> **前一轮交接（2026-09-11）**：v3.0 五阶段演示骨架完成流程正确性优化。mock 提供方仍不能生成真实内容，但会明确标记 `mode: mock` / `productionReady: false`；模拟渲染不会登记虚假媒体或正式成片。详细变更、验证与遗留边界见 [09-优化落地记录.md](09-优化落地记录.md)。

> 交接文档。写给下一位接手这个仓库的开发者：读完这份文档，你应当能在本机把项目跑起来、看懂代码落在哪、知道哪些是真的、哪些还是占位、以及下一步从哪里开工。
>
> - 交接日期：2026-09-16（v3.1 修复批次 + 远程合并）
> - 当前迭代：v3.1 五阶段演示骨架 + 安全与正确性修复（已含远程第二轮审查落地）
> - 权威需求文档：[04-详细优化方案.md](04-详细优化方案.md)
> - 变更记录：[09-优化落地记录.md](09-优化落地记录.md)

---

## v3.1 修复批次（2026-09-15）

本批次基于全量代码审查（基线 `d925a3e`，20 项发现）落地了可在不接入真实外部服务前提下独立修复的缺陷。三个修复组：

### ① 工程存储安全（后端）

- **路径与 ID 校验**（`storage/project_store.py`）：`_path()` 强制 ID 为 `[A-Za-z0-9_-]+`（1–255 字符），拒绝 Windows 设备保留名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）；`Path.resolve()` + `is_relative_to()` 防穿越；拒绝根内链接别名（同一工程不能用不同 ID 访问）。`load()` 缓存命中前也走校验，并验证 `proj.id == project_id`。
- **原子写加固**：`flush()` 改用 `NamedTemporaryFile`（`prefix=".project-"`），不再跟随攻击者预置的 `project.json.tmp` 链接；`os.replace` 失败保留旧文件，`finally` 清理临时文件。`jobs/manager.py` 的 `_persist()` 同步升级（`prefix=".job-"`）。
- **创建冲突语义**：`create()` 对已存在目录 / 占位文件 / 坏 JSON / 缓存冲突抛 `ProjectAlreadyExists` → API 409；自动编号取「已落盘工程 ∪ 根目录所有子项」的 `EP\d+` 最大值 +1（占位目录也占号，防止覆盖）。
- **输入校验**：`apply()` 拒绝非 dict patch、`id` 不可变、非 int/负数 `expectedRevision`；非法 ID/路径统一 `InvalidProjectInput` → `main.py` 异常处理器映射 422。
- **打包修复**（`pyproject.toml`）：`packages.find` 替换固定 `packages = ["duocast"]`，wheel 现在包含全部子包。
- **测试**：新增 `tests/test_project_safety.py`（497 行），覆盖非法 ID 全操作拒绝、合法 ID round-trip、重复创建 409、占位路径拒绝、resolve 越界受控、符号链接/hardlink 不读写、`os.replace` 失败保留旧文件、API 422 映射、默认编号算法、PATCH 不改 id 等 372 用例。
- **已知环境限制**：本机 Windows + Python 3.13 的 `symlink_to()` 可能静默不创建链接（不抛异常），测试辅助 `make_symlink()` 创建后会校验存在性，失败即 `pytest.skip`（当前 7 skipped）。FastAPI 0.141+ 的 `include_router` 把业务路由包在 `_IncludedRouter` 里，测试需递归展开（`_flatten_api_routes()`）。

### ② 保存-确认竞态与配音死锁（前端 + 后端）

- **保存屏障**（`EditScriptPage.tsx`）：新增 `flushDraft()`——确认脚本 / 提交改写前，先把挂起的 2s 防抖保存立即执行并等待结果。旧实现闭包捕获过期 `proj.revision`，编辑后立刻确认必然 409（expectedRevision 落后），且 `handleConfirm` 不等保存完成，双写竞态会丢编辑。防抖回调现在从 `qc.getQueryData` 读最新工程快照，不再用旧闭包 revision。
- **保存状态四态**：底部统计条与确认按钮显示「未保存 / 保存中 / 已保存 / 保存失败」，替代固定「已保存」文案；保存中禁用确认与「下一步」。
- **配音回改死锁解除**（`VoicePage.tsx`）：`canSynthesize` 移除 `!voiceApproved` 条件——旧逻辑下脚本回改后时间轨过期（`timeline.revisionId != currentDraftRevision`），确认记录仍指向旧版本导致按钮永久禁用，用户无路可走。现在已确认的配音允许重新生成（后端 `voice_svc.synthesize_timeline` 会清除旧 voice/sample 确认并写入新时间轨，语义安全）。
- **时间轨过期显式化**：新增 `timelineStale` 派生，页面顶部显示警示、按钮文案变为「重新合成（脚本已改动）」、时长检查条 Badge 显示「时间轨过期」。
- **gap 编辑定位修复**：转场间隔滑杆按 `selectedUnitId` 对应话轮的真实相邻区间写入（旧实现固定写 `i===0`，拖谁都是改第一段）。
- **任务终态时刻**（后端）：`Job` 域模型新增 `finished_at`（对外 `finishedAt`）；`transition()` 进入 `succeeded/failed/cancelled` 时打戳、回到进行中状态清空、非法转换不动戳。前端 `elapsedOf` 终态任务耗时 = `finishedAt - createdAt`（不再无限增长），任务卡显示「完成」时刻，检查器增加完成行。
- **测试**：`tests/test_optimization.py` 新增 4 组用例（终态打戳、暂停非终态、进行中落盘 `finishedAt` 为空、provider 别名映射）。

### ③ 显示口径清理（前端 + 后端）

- **provider 测试别名**（`api/providers.py`）：`textApi` / `imageApi` 作为 `text` / `image` 的规范别名接受，响应返回 canonical ID。旧实现前端「测试连接」发 `textApi`/`imageApi`，后端只认 `text`/`image`，返回 `{ok:false, error:"unknown provider"}`，但能力面板仍显示「已连接」——按钮形同虚设。
- **去掉假读数**（`ServicesPage.tsx`）：删除硬编码 `usedVram = 2.8 / totalGb = 16` 显存条（后端 machine.json 不提供实测占用），改为「演示模式 · 不占用真实显存」Badge 或「实际占用待本地引擎接入后测定」；删除「2 个绑定有效」假计数（后端无绑定数量接口），改为「绑定能力开放 / 未配置」。mock 模式标识沿用能力快照的 `mode: 'mock'`。
- **任务中心刷新**（`TasksPage.tsx`）：刷新按钮真正调用 `jobs.refetch()` 并显示 `isFetching` busy 态（旧实现只改本地 `refreshedAt`，不发请求）。

### v3.1 验证记录（合并后，2026-09-16）

- 后端全量：`pytest tests/ -q` → **411 passed, 7 skipped**（7 skipped 均为符号链接环境限制）。
- 前端：`tsc --noEmit` 零错误；`vite build` 通过（`dist/assets/index-*.js` 304.59 kB / gzip 93.31 kB）。
- 未接入任何真实外部服务；所有测试使用临时目录；mock 结果不冒充真实产出的显示口径保持。

### 与远程 806f654 合并（2026-09-16）

本地 v3.1（`3f01959`）与远程第二轮审查落地（`806f654`）rebase 合并为 `a3177ce`，6 处冲突手工解决，策略为**保留双方有效修复**：

- **`project_store.py`**：保留远程 `debounce_ms=500` + 本地 `root.resolve()`（防链接别名）与本地的 `_path()`/`apply()`/`flush()` 安全版本。
- **`api/projects.py`**：保留远程 `PROJECT_ID_RE`（允许中文/空格）、`ALLOWED_PATCH_FIELDS` 白名单、确认绑定校验，叠加本地的"占位目录也占号 + 409 重试"编号逻辑与显式 ID 校验。
- **`EditScriptPage.tsx`**（4 处）：远程 `baseRevId` 乐观锁 + 本地 `flushDraft` 保存屏障/四态显示合并；确认按钮叠加远程的 `!viewIsDraft` 门控与本地的 busy 态。
- **`VoicePage.tsx`**：远程 `timelineStale` 显式化 + 本地的 gap 编辑定位修复；`canSynthesize` 采纳远程 P2-16 语义（已确认可重做，仅显式标注过期）。
- **`HANDOFF.md`**：双方状态与章节合并。

**合并后测试契约对齐**（远程语义为准，本地测试已改写）：

1. **ID 字符集**：`project_store._path()` 正则扩展为与 API 层 `PROJECT_ID_RE` 对齐——允许中文与内部空格（`test_review_fixes.py::test_project_id_still_accepts_chinese_and_spaces` 要求）。本地额外加固保留：拒绝首尾空白（Windows 会剥离目录名尾部空格，`"EP001 "` 与 `"EP001"` 会落同一目录造成覆盖），保留设备保留名拒绝与 resolve/containment 检查。测试中 `"EP 001"` 移入合法列表，新增 `"中文工程"`、`"EP 中文01"`、`"第1期_嘉宾访谈"` 覆盖。
2. **`id: null` 语义**：API 层显式 `id=null` 等价于不传（走自动编号），仅 store 层拒绝 `None`。`test_create_api_rejects_explicit_invalid_id` 改用排除 `None` 的 `API_INVALID_IDS`，并新增 `test_create_api_treats_null_id_as_auto_numbering`。
3. **PATCH 字段白名单**（远程 P2-5）：`id`/`revision`/`updatedAt`/`audioTimeline` 等内部字段注入返回 422——这是正确的服务端防护，原测试通过 PATCH 写这些字段验证命名兼容的做法不再成立。改写为双断言：先验证注入被拒（422 + 具体字段名），再验证合法业务字段（`title`/`currentDraftRevision`）正常写入；`audioTimeline` 注入单独成测试。

### v3.1 已知剩余问题（下一批次候选）

- `EditScriptPage` 的确认仍依赖「flushDraft 成功」这一本地屏障；若保存失败用户强行刷新页面，未落盘编辑会丢（浏览器无 beforeunload 拦截）。
- `VoicePage` 单元级「重新生成 / 拆出一句 / 新旧对比」按钮仍是 disabled 占位（需后端单元级重合成 API）。
- `jobs/manager.py` 的 `set_stage()` 不打 `finished_at`（只走 `transition()` 才打）——当前所有终态都经 `transition()`，无实际影响，但新增终态路径时要记得走 `transition()`。
- 插入句内停顿按钮 disabled 占位（引擎语法未验证）。

---

## 一句话现状

产品五阶段流程（创建本期 → 编辑对话 → 试听配音 → 预览画面 → 生成导出）的**前后端骨架已经全部打通并可运行**：九屏页面、任务状态机、事件推送、版本存储、确认点都在真实工作；**九屏 UI 已按高保真设计稿 `hf/HF-00 ~ HF-08` 完成类名与结构对齐**。

但四个内容提供方（文本 / 图片 / 语音 / 视频）目前是 **mock 适配器**，返回占位数据，**还不能产出真实的脚本、图片、音频和成片**。

换句话说：流程和编排、输入快照、任务恢复、确认失效与界面版式已落地（两轮加固后还包括：确认绑定校验、409 冲突重放、工程 ID 白名单、SSE 补发缺口检测）；内容生成仍是演示数据。模拟数据不能被标记为真实音频、图片或成片。

---

## 仓库结构

```
duo-podcast/
├── 01-页面详细设计文档.md
├── 02-项目实施方案文档.md
├── 04-详细优化方案.md          # ★ 当前权威需求（v3.0 五阶段）
├── 05-UI设计系统与页面规划.html # 设计令牌、组件规范、前端落地映射
├── 06-后端设计规划.md
├── 07-代码与UI交互审查报告.md   # 代码 / UI / 交互审查（P0/P1 缺陷清单）
├── 08-UI对齐专项审查与修复报告.md # UI 对齐根因分析与修复记录
├── hf/                         # 九屏高保真设计稿（HF-00 ~ HF-08，静态 HTML）
│   └── vendor/duo-shell.css    # 设计稿自带的壳样式（比对基准）
├── apps/
│   ├── server/                 # 后端 FastAPI 服务
│   ├── web/                    # 前端 React + Vite
│   └── storage/                # 运行时数据（gitignored，不入库）
└── HANDOFF.md                  # 本文件
```

文档的单一权威原则：同一条规则（时长口径、双音轨、失效矩阵、确认策略）只在 04 及其指向的 01/02 章节定义一次，其他文档引用，不要在代码注释里另立口径。

---

## 本地运行

环境：Windows、Python ≥ 3.10、Node 22 + pnpm。

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

### ⚠️ 首次运行必读：两个环境陷阱

1. **HTTP 代理会拦截 localhost 请求**。若本机设置了 `HTTP_PROXY/HTTPS_PROXY`（本机实测有 `http://127.0.0.1:23134`），对 `127.0.0.1` 的请求会被转发到代理，表现为 **502 Bad Gateway** 或 TLS 报错。启动服务或调用本地 API 前先绕过：

   ```bash
   env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY NO_PROXY="127.0.0.1,localhost,::1" \
     python -m uvicorn duocast.main:app --host 127.0.0.1 --port 8100
   ```

2. **`smoke_e2e.py` 不会自启服务**。它硬编码请求 `http://127.0.0.1:8100/api`，必须先手动把后端起在 8100 上，否则报 `WinError 10061 目标计算机积极拒绝`。且后台进程要与其同在一个 shell 会话内启动，否则会随上一条命令结束被回收。

---

## 代码地图

### 后端 `apps/server/duocast/`

| 目录 | 文件 | 职责 |
|---|---|---|
| `core/` | `config.py` | 仓库根、端口、存储根、队列上限（端口可用 `DUOCAST_PORT` 覆盖） |
| | `eventbus.py` | 进程内事件总线，事件带序号、落 `events.jsonl`，SSE 推送 |
| | `logging.py` | 结构化 JSON 日志 |
| `domain/` | `project.py` `audio.py` `visual.py` `render.py` `job.py` | Pydantic 领域模型（工程、话轮、音频时间轨、画面变体、输出版本、任务）。`Project` 含 `updatedAt` |
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
| `components/wu.tsx` | workspace-ui 基线组件库（Button/Card/Dialog/Badge/Input/Modal 等，`wu-` 命名空间）。`Icon` 用 `data-size` 刻度（xs 12 / sm 16 / 默认 20）驱动 CSS，不再依赖 SVG `width/height` 属性 |
| `components/AppShell.tsx` | 应用壳。导出 `TopBar` / `StageRail` / `StageHead` / `TimelineBar` / `FootBar` / `AppShell` / `fmtDur`。`AppShell` 有三个插槽：`inspector`（第三列）、`timeline`、`footer` |
| `lib/api.ts` | API 客户端 + TanStack Query hooks（查询/变更） |
| `lib/sse.ts` | EventSource 封装，断线重连、`system.resync` 全量回读 |
| `lib/types.ts` | 与后端对齐的 TypeScript 类型 + `STAGE_META` / `JOB_LABEL` / `PROVIDER_META` |
| `stores/index.ts` | Zustand：projectStore（当前工程/视图）、jobStore、serviceStore（能力门控） |
| `styles/tokens.css` `components.css` `duo-shell.css` | 设计令牌、组件样式、布局（`duo-shell.css` 镜像设计稿 `hf/vendor/duo-shell.css` 的 `hf-*` 类） |
| `pages/` | 九屏：HomePage、CreateEpisodePage、EditScriptPage、VoicePage、VisualPage、RenderPage、AssetsPage、ServicesPage、TasksPage |

---

## 应用壳（AppShell）的三条硬规则

这三条是 UI 对齐的核心结论，改任何页面时都必须遵守，否则会重演此前「检查器位置错、底栏起点右移 208px」的问题。

1. **`.hf-app` 的子元素顺序固定为 `[hf-top, hf-mid]`，可选追加 `hf-timeline`、`hf-foot`。**
   `TimelineBar` 与 `FootBar` 是 `.hf-app` 的**直接子元素（全宽）**，不是 `.hf-main` 的子元素。此前实现在 `.hf-main` 里嵌了两者，导致底栏起点右移了阶段轨的 208px。

2. **`inspector` 是 `.hf-mid` 的第三列**，与 `stageRail` / `hf-main` 并排，不是 `.hf-body` 的子元素。此前用 `position:sticky` 补偿，导致检查器顶部比阶段标题低一档、底部被 FootBar 切齐。

3. **`footer` 只在阶段页出现。** 设计稿中 `hf-foot` 的分布是：

   | 屏 | 是否有 FootBar | 说明 |
   |---|---|---|
   | HF-00 工程首页 / HF-01 创建本期 | 无 | 独立全屏 |
   | HF-02 ~ HF-05 阶段页 | **有** `<footer class="hf-foot">` | 阶段流程 |
   | HF-06 资产库 / HF-07 服务设置 | 无 | 独立全屏入口 |
   | HF-08 任务中心 | 无 FootBar，但正文内有 `.hf-foot-note` | 独立全屏入口 |

   `AppShell` 的 `footer` 传 `undefined` → 渲染默认 `FootBar`；传 **`null` → 显式不渲染**。HF-06/07/08 三页必须传 `null`。

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

### 后端

- 工程存储 + revision 乐观锁 + 防抖落盘；不可变资产登记；机器设置
- 任务状态机（幂等 clientToken、三队列、暂停/取消、启动恢复扫描）
- 五条任务链路：脚本生成、候选改写（不落盘，用户接受后才替换）、配音合成、画面生成、渲染导出
- 工程域含音频时间轨（48kHz 母轨、整数样本偏移、单元偏移表）、画面变体、输出版本、三个确认点 approvals
- SSE 事件流 + 前端实时推送
- 第二轮加固（2026-09-12，[11-代码审查报告.md](11-代码审查报告.md) 全部 P0/P1/P2 落地）：PATCH 字段白名单、三个默认确认点强制绑定当前草稿、工程 ID 白名单（防路径穿越/重复 409）、重复取消幂等且不再广播假失败事件、损坏任务清单生成 unknown 占位、SSE 补发缺口检测 + resync 不带 id + 慢消费者队列清空、事件日志 16MB 轮转、防抖 500ms、visual 任务接入图片提供方并在 await 后重读工程、script 生成不登记假 artifactId、sourceInput kind 三入口透传

**端到端冒烟（2026-09-11 修复后复跑）**：`ALL PASS` —— 机器设置 → 建工程 → 脚本生成(R-uuid, 3 turns) → 确认脚本 → 配音合成(157440 samples / 3 units) → 确认配音 → 画面生成(V01) → 样片确认 → 渲染导出（演示模式，不登记假视频）→ 资产清单(0 项，不登记假资产) → 机器设置保存 → 提供方测试。

**自动回归**：`pytest tests -q` → **35 passed**（22 项契约回归 + 13 项审查修复回归 `test_review_fixes.py`）；场景验证 12 项（`tests/scenario_checks.py`，临时目录隔离）全绿。

### 前端

- 九屏全部接入真实数据流（TanStack Query + SSE）
- **`tsc --noEmit` 零错误 · `vite build` 通过**（2026-09-11 复跑，产物 `dist/` 302.72 kB / gzip 92.55 kB）
- 第二轮加固（11 报告）：查看历史版本时编辑新建草稿、绝不覆盖当前草稿（`upsertDraftTurns` baseRevId）；保存/确认 409 冲突重放 + 错误提示（`useSaveDraft`/`useConfirm`）；任务卡片操作绑定卡片自身任务（`TasksPage`）；确认按钮限当前草稿；VoicePage 间隔滑杆读写同一索引、已确认后可重新合成；旧改写候选不再复弹；stale 阶段不复用 done 高亮
- 九屏按设计稿完成类名迁移：Pages 内联 `style` 基本清零，改用 `duo-shell.css` 中的 `hf-*` 设计类
- 类名词汇命中率从 45~78% 提升到 **88~100%**（逐页 `vocab-audit` 验证）
- 壳尺寸与设计稿一致：TopBar 56 / 阶段轨 208 / 检查器 320

### UI 对齐已修的四类根因（详见 08 报告）

| 根因 | 现象 | 修法 |
|---|---|---|
| ① 基线选择器优先级 `(0,1,1)` 压过设计类 `(0,1,0)` | `.wu :is(button,…){font:inherit}` 因键盘可达把 `<div>` 改成 `<button>` 后命中 | 设计类提升特异性 / 避免用 `<button>` 承载纯版式容器 |
| ② SVG 的 `width/height` 属性被 CSS 覆盖 | `Icon` 的 `size` 参数全域失效 | 改用 `data-size` 刻度驱动 CSS |
| ③ 设计类未移植，页面用内联 `style` 重写第二套版式 | 字号/圆角/间距系统性漂移 | 把 `hf-*` 类补进 `duo-shell.css`，页面改用类名 |
| ④ 设计系统自身缺口 | `.wu-icon-btn` 无外观重置、`.wu-alert` 无 `muted` | 在 `duo-shell.css` 补齐 |

---

## 下一步开工顺序

按 02 的交付关口制，骨架已具备，接下来进入「让内容生成变真」的阶段：

1. **真实提供方适配器**（最高优先）。契约在 `adapters/base.py`，新增实现后在 `main.py` 替换对应 mock。建议顺序：文本 API → 图片 API → 本地 TTS / MiniMax → ComfyUI 视频。凭据存储接 keyring（关口 B）。
2. **关口 A 硬件实测**（视频侧的硬门槛）。验证 InfiniteTalk/MultiTalk 双轨输入适配（官方 `add` 是串行拼接，不能直接当双轨相加）、16GB 显存跑 480p 双人的可行性、跨段边界连续性。这一项不通过，视频页不进入完全实现。
3. **前端可视化库接入**：WaveSurfer（时间轨波形/试听）、Konva（人物区域标注）、dnd-kit（话轮拖拽排序）。目前时间轨是 CSS 条形占位。
4. **读音词典服务端持久化**；单元/集成测试补全（现有 `tests/` 仅冒烟）。
5. 真实音频/图片产物的文件落盘与下载路径（当前资产登记只写元数据，无实体文件）。
6. ~~07 报告中的 P0/P1 缺陷~~ → 已在 2026-09-11 第二轮修复中清零（见 11 报告 §7 修复路线）；剩余 07/11 的 P2 项（线程安全加固、资产登记链路设计、词典持久化等）随迭代处理。

---

## 已知坑与约定

- **防抖落盘窗口**：PATCH 返回 200 后，落盘经 500ms 防抖（`project_debounce_ms`，11 报告 P2-9 已从 2s 收窄）。kill -9 窗口内仍会丢最近一次编辑；前端 `useSaveDraft`/`useConfirm` 带 409 冲突重放作为补偿。
- **工程 ID 白名单**：POST /api/projects 的 id 允许字母/数字/空格/连字符/下划线/中文（不含路径分隔符与点号），重复 id 返回 409；PATCH 字段有白名单，三个默认确认点必须绑定当前草稿（11 报告 P1-4/P1-6/P2-5）。

- **仓库根路径**：`core/config.py` 用 `Path(__file__).resolve().parents[4]` 定位仓库根（文件在 `apps/server/duocast/core/` 下，向上 4 层才是仓库根）。曾误写成 `parents[3]` 导致定位到 `apps/`，静态资源挂载到不存在的 `apps/apps/web/dist`，表现为 8100 只返回「前端未构建」占位 JSON。改动这块路径后务必重启后端并验证 `GET /` 返回的是 HTML 而非 JSON。
- **HTTP 代理拦截 localhost**：见上文「首次运行必读」。本地调试出现 502 优先排查这个。
- **设计稿有整体缩放**：`hf/*.html` 的 `.frame` 带 `transform: scale(0.7944)`，`getBoundingClientRect()` 读出的值比真实值小 20%。做版式比对必须用 **`getComputedStyle()`**，或用 `.frame` 元素本身的尺寸，否则会得出错误的"差异"结论。
- **设计系统自身存在内部不一致**：例如 `hf-fld label` 在 HF-05/06 写 12.5px、在 HF-07 写 12.8px。遇到这类冲突以「跟声明值」为准并记录下来，不要在两处之间反复横跳。
- **运行时数据不入库**：`apps/storage/`（工程、任务、资产、事件、机器设置）已在 `.gitignore`，换机器或重置时删除该目录即可回到空状态。
- **存储根**：默认 `apps/storage`，可用环境变量 `DUOCAST_STORAGE` 覆盖。
- **端口**：后端 8100（`DUOCAST_PORT` 可改），前端 dev 5178（vite `strictPort`）。
- **时长权威**：节目总时长以最终 48kHz PCM 母轨样本数为准，不是最后一句时间码；改 TTS 适配器时必须返回真实 `sampleCount`。
- **mock 与真实不要混用口径**：替换适配器时保持 `capabilities` 字段与实际能力一致，前端门控依赖它。
- **`.review/` 是本地审查工作区**：截图、审查脚本、临时存储副本都在这里，已在 `.gitignore`。里面的脚本（`vocab-audit.mjs`、`shot-final.mjs`、`full-audit.mjs` 等）可直接当回归用例复用。

---

## 常用命令速查

```bash
# 重启后端（改了 Python 代码后），并绕过本机代理
cd apps/server
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  python -m uvicorn duocast.main:app --host 127.0.0.1 --port 8100

# 前端类型检查 + 构建
cd apps/web && pnpm build

# 端到端冒烟（必须先把后端起在 8100，再跑）
cd apps/server && python smoke_e2e.py

# 自动回归（35 项：契约 + 审查修复）
cd apps/server && .venv/Scripts/python.exe -m pytest tests -q

# 场景验证（12 项边界场景，临时目录隔离，不碰真实工程数据）
cd apps/server && .venv/Scripts/python.exe tests/scenario_checks.py

# UI 对齐回归（本地审查脚本，需 8100 应用 + 8199 设计稿静态服务）
python -m http.server 8199            # 仓库根，供 hf/ 设计稿访问
node .review/shot-final.mjs           # 三张独立全屏页的结构与截图复核
node .review/vocab-audit.mjs          # 逐页类名词汇命中率
```
