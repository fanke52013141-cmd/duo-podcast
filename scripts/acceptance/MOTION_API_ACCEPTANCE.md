# 正式本机 API 分阶段验收

`run_motion_api_acceptance.py`只访问显式指定的本机HTTP服务，拒绝重定向和外站。默认`plan`只读输入文件并计算摘要，不发HTTP、不启动模型。请使用隔离的服务数据目录；配置、状态和成片保留在`.review/`，其中可能包含私有剧本和本地输入信息，不要提交仓库。

配置示例结构（文件名为占位，需换成实际素材）：

```json
{
  "name": "物品角色验收",
  "characters": {
    "A": {"characterId": "lamp", "name": "台灯", "objectType": "articulated_object", "referenceImageFile": "lamp.png"},
    "B": {"characterId": "signal", "name": "红绿灯", "objectType": "articulated_object", "referenceImageFile": "signal.png"}
  },
  "cameras": [{"id": "dual", "mode": "twoShot", "masterImageFile": "dual.png"}],
  "clips": [{
    "id": "dual-A-speaking", "role": "A", "cameraId": "dual", "state": "speaking",
    "videoFile": "dual-A.mp4", "coordinatesFile": "dual-A-boxes.json",
    "usableStartFrame": 0, "usableEndFrame": 100,
    "bodyRegion": {"x": 0, "y": 0, "width": 0.5, "height": 1},
    "inspection": {
      "bodyRegions": [{"x": 0.05, "y": 0.5, "width": 0.35, "height": 0.35}],
      "exclusionRegions": [{"x": 0.1, "y": 0.1, "width": 0.3, "height": 0.35}],
      "backgroundRegion": {"x": 0.45, "y": 0, "width": 0.1, "height": 1}
    }
  }],
  "shotStyle": "twoShot",
  "scriptText": "A：你的关节为什么可以动？\nB：我用支架轻轻回应。\nA：我们再看一下反打镜头。",
  "voiceOptions": {"transitionGapMs": 200},
  "renderOptions": {"resolution": "1280x720", "fps": 25, "fastQuality": "clear"},
  "preparation": [{"cameraId": "dual", "role": "A", "state": "listening", "seed": 42, "durationSec": 4, "prompt": "根据实际关节做轻微动作，保持五官可见"}]
}
```

示例仅展示一条片段，因此故意不满足整包就绪条件。同框需要A/B各自说话和倾听素材；双向过肩需要两个真实机位及各可见主体的状态素材。检查区域必须按实际画面重新标注，不能照抄示例。坐标文件是`boxes`数组或`{"boxes": [...]}`，长度等于源帧数，每帧为原生像素`[x1,y1,x2,y2]`；选区外可为null。台灯/红绿灯只是类型示例，不构成效果保证。

从仓库根执行，所有阶段使用相同配置、状态和base-url：

```powershell
apps/server/.venv/Scripts/python.exe scripts/acceptance/run_motion_api_acceptance.py --config .review/api/config.json --state .review/api/state.json --base-url http://127.0.0.1:8137 --stage plan
```

随后依次执行`--stage import`、`coordinates`、`check`。工具记录注册引用、SHA256、原生规格、HTTP状态、冻结请求摘要及服务端检查结果。检查完成只进入待人工审看；不会自动采用。

人工审看后另写reviews文件，`clips`数组各项必须含`clipId`、严格布尔`accepted`、非空`reason`。只在操作者已观看对应区间并独立填写结论后，执行`--stage review --reviews .review/api/reviews.json --apply-reviews`。服务器硬失败不能由理由覆盖。不要填入模板结论冒充真实审看。

`--stage project`先查询指定镜头风格就绪状态，再保存固定动作包版本。之后分阶段执行：

1. `script --allow-model-calls --wait-seconds 120`：导入显式编写的剧本并记录任务。
2. `confirm --confirm-kind script --reviews ... --apply-reviews`：操作者对当前剧本确认。
3. `voice --allow-model-calls --wait-seconds 1200`：真实配音。
4. `confirm --confirm-kind voice --reviews ... --apply-reviews`：完成真实配音试听后确认。
5. `sample --allow-model-calls --wait-seconds 1200`，随后`verify`下载登记视频并验证解码、规格、音轨。
6. `confirm --confirm-kind sample --reviews ... --apply-reviews`：操作者逐项审看口型、身份、倾听和切点。
7. `render --allow-model-calls --wait-seconds 1200`及`verify`：完整成片和媒体证据。

`reviews.project`为独立人工输入，例如`{"kind":"voice","note":"实际试听后填写的判断"}`。样片项还需要`checks`包含操作者确实检查过的“口型与发言一致”“人物身份稳定”“倾听反应自然”“切点连续”。工具不会自动补齐这些检查。没有当前人工确认会保存pending并停止相应后续步骤。模拟或来源未知的配音/样片不能作为真实媒体确认。

可选`prepare --allow-model-calls`只提交配置中明确列出的真实I2V任务，保存原始任务ID；不会自动导入或采用候选。服务端成功候选的media/native/provenance应由操作者审看后补入新配置/草稿。超时不会重新POST；再次执行同阶段只查询原任务。若POST响应丢失，先用`--stage reconcile --job-key prepare-0`按原token、owner与kind只读找回任务。没有唯一匹配时保持unresolved，禁止盲重采样。已知providerTaskId的任务可显式`resume --job-key prepare-0 --allow-model-calls`调用服务端续查入口。

`verify --compare-state .review/other/state.json`可对另一份已验收媒体账中的完整成片做解码PCM比较，用于同框/过肩共用配音验证。下载只接受登记artifact ID，流式限制512MiB，实际内容必须匹配注册SHA256；已有证据被改动时停止，不覆盖。短样片与完整成片时长不同则不宣称音轨一致。

所有阶段始终保留`fullAcceptancePassed=false`。该工具提供正式API与媒体事实，不能证明连续自然度、完整五官可见性、任意物品成功或整个T01–T12目标完成。
