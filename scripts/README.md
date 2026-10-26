# DuoCast 本机运行与维护

## 启动服务

服务与 GPU 引擎使用独立 Python 环境。下面命令在仓库根的 PowerShell 执行；需要 Python 3.12、Node 和 FFmpeg/ffprobe 在 PATH 中。

```powershell
python -m venv apps/server/.venv
apps/server/.venv/Scripts/python.exe -m pip install -r apps/server/requirements-tested-windows.txt
apps/server/.venv/Scripts/python.exe -m pip install --no-deps -e apps/server
```

前端已安装依赖时，可直接执行与 package.json 相同的检查/构建，避免环境包装器额外安装依赖：

```powershell
Set-Location apps/web
node --test tests/workflow.test.cjs
node node_modules/typescript/bin/tsc --noEmit
node node_modules/vite/bin/vite.js build
Set-Location ../..
```

首次需按 `apps/web/pnpm-lock.yaml` 安装前端依赖；如所用包管理器要求批准 esbuild 的构建脚本，应按包管理器提示审查官方 esbuild 构建，不修改项目配置来绕过策略。

启动纯演示（即使机器保存了真实配置，也强制本次进程使用 mock）：

```powershell
./scripts/start.ps1 -Mode mock
```

访问 `http://127.0.0.1:8100`。启动器前台运行，可用 Ctrl+C 正常退出；不会自动启动或安装 GPU 引擎。

## 真实配置

1. 从 `scripts/runtime.example.json` 复制本机配置到仓库外，例如 `C:/DuoCast/runtime.local.json`。该示例采用 DUOCAST 环境变量结构，**与旧 digital-human 启动器的 engineHome/runtimeRoot 结构不同，不要混用或覆盖旧配置**。
2. 修改引擎地址、input、A/B 参考音频和快速运行目录。模型、第三方运行环境、密钥、媒体不入 Git。
3. 在系统环境中设置 `DUOCAST_TOAPIS_KEY`；本机配置文件只存路径和配置，避免放密钥。
4. 启动现有 ComfyUI 服务后运行：

```powershell
./scripts/start.ps1 -Mode configured -ConfigPath C:/DuoCast/runtime.local.json
```

也可在首页“环境检查与服务设置”保存现有适配器的地址、模型、提供方、凭据环境变量名和预算，重启服务后生效。服务设置中的地址是根地址，例如 `https://toapis.com`，不附加 `/v1`。

机器设置保存在 `DUOCAST_STORAGE/machine/machine.json`（默认 `apps/storage/machine/machine.json`）。页面保存的运行设置优先于相应进程默认值；以页面“当前配置”与诊断结果为准。纯演示启动参数优先于保存的真实配置。

旧 `scripts/digital-human/start.ps1` 保留 engineHome/runtimeRoot 配置与既有引擎自启流程，已移除对私人 `_run_dev_toapis.cmd` 的依赖。它需要旧结构配置，适合已有数字人运行目录的机器；新部署优先使用通用 `scripts/start.ps1`。

## 环境检查的边界

- `GET /api/diagnostics`：Python、FFmpeg/ffprobe 路径、参考音频文件、快速运行时文件、磁盘空间和最近检测结果。
- `POST /api/providers/{text|image}/test`：请求模型清单，检查凭据与所选文本模型；不会生成付费内容。
- `POST /api/providers/{tts|video}/test`：检查引擎在线及所需节点；不加载权重，不证明推理、人物身份或口型质量通过。
- 演示检测会明确标记；请求真实通道但缺密钥回落演示时，检测返回失败提示。
- 预算目前是持久化软提醒配置，实际费用未接可靠账单；不得将未知费用当作零费用或硬限额保证。

## 快速模板、高清素材与版本管理

旧工程仍使用原有模式；新快速工程会固定素材及 worker 文件版本和运行目录。更换人物应准备新的完整目录，保留旧目录，不能直接覆盖正在被旧工程使用的 assets/worker。

素材准备、高清尺寸与坐标校验沿用 `scripts/fast-mode/README.md` 中的命令。完整运行目录可注册到本机目录清单：

```powershell
apps/server/.venv/Scripts/python.exe scripts/fast-mode/catalog.py --runtime C:/DuoCast/studio-v2 --catalog C:/DuoCast/templates.json --label "演播室第二版"
# 人工观看身份、口型、手部、倾听与接缝后，才重新登记 --reviewed
apps/server/.venv/Scripts/python.exe scripts/fast-mode/catalog.py --runtime C:/DuoCast/studio-v2 --catalog C:/DuoCast/templates.json --label "演播室第二版" --reviewed
$env:DUOCAST_FAST_CATALOG='C:/DuoCast/templates.json'
```

新建快速节目可选择就绪模板；旧工程继续绑定原版本。仅声明 `reviewed` 不构成自动质量保证。目录清单中“已审看且文件版本一致”的模板才能选择；模型推理仍需本机验证。

只有旧 `DUOCAST_FAST_ENGINE` 时兼容现有单模板方式，视为用户先前已采用的本机模板；新素材建议使用清单并显式审看。当前仓库未提供新的高清正式动作库，720p/1080p 输入与长片稳定性需真实资源到位后验收。

## 工程备份与清理

- 工程卡/生成页“备份工程”下载工程与引用媒体；与 MP4/WAV/SRT 媒体交付包分开。
- 首页“导入工程”只接受工程备份格式，始终另存新 ID，不覆盖原工程；校验媒体哈希并重定位路径。模型、引擎、密钥与外置模板不在包中。
- 迁移后声音/样片确认需重新检查；缺失媒体列表会返回。超过 2GB 的包请使用完整本机目录备份并保留外置依赖，当前上传入口不接收超大包。
- 首页“工程恢复与空间管理”可恢复软删除工程、查看引用媒体占用、将查找缓存移入回收区并恢复。
- 已有同 ID 工程时恢复被拒，先备份并另存；不会静默覆盖。
- 只清理可重建的查找缓存；采用媒体、视频片段和模型缓存不自动删除。进行中任务存在时维护操作会被拒绝。

## 验证命令

```powershell
Set-Location apps/server
./.venv/Scripts/python.exe -m pytest tests -q
./.venv/Scripts/python.exe tests/scenario_checks.py
# 需已启动隔离存储的 8100 服务；不要对生产工程目录做冒烟
./.venv/Scripts/python.exe smoke_e2e.py
```

这组测试不替代真实 GPU 推理与窗口视觉验收。测试记录和待验收范围见仓库根 `35-三批优化落地与验证.md`。
