# 动作工作流开发进展

完整范围与门槛见[IMPLEMENTATION.md](IMPLEMENTATION.md)。

## 批次0：固定当前基线

- 起点：`105c7f64f52c8cb17faf19ef9f2113cf4319bf9d`，开始前工作区干净，与origin/main一致。
- 后端：在apps/server执行`.venv/Scripts/python.exe -m pytest tests -q`，611 passed / 7 skipped，31.02秒；两个既有第三方弃用提示。
- 前端：11项workflow测试通过，TypeScript无错误，Vite构建成功。
- 新建完整T01–T12、V01–V18总账；各功能任务仍标为未完成。
- 资产域/存储、素材检查、独立验收工具已分派并行开发；尚未作为已完成功能计账。

本批是可追溯基线和完整范围记录，不代表任何新模型效果或产品功能已完成。
