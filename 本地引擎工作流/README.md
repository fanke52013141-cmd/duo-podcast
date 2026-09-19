# 本地引擎工作流（ComfyUI 参考副本）

本目录是 **只读参考副本**，供 DuoCast 接真实 TTS / 数字人引擎时对照用。
原始文件不属于本项目，改动本目录不影响任何运行中的环境。

## 两份工作流

| 文件 | 用途 | 跑在哪套 ComfyUI |
|---|---|---|
| `indextts2.5-tts_参考工作流.json` | 阶段③语音合成（IndexTTS 2.5，BSAI 节点，API 格式） | 主包 或 PPT Runtime 均可（都有 BSAI 节点） |
| `infinitetalk-数字人_api_windows-compatible.json` | 阶段④⑤数字人口型（MultiTalk / WanVideoWrapper，API 格式） | PPT Runtime（WanVideoWrapper + MultiTalk + 内核 PrimitiveFloat 齐全） |

## 本机 ComfyUI 布局与「唯一实体」事实（2026-09-19 只读取证）

**大模型只有一份物理实体，其余都是链接——之前担心的「重复占盘」不存在。**

```
主包（唯一实体，模型真身）
  D:\软件\Wan2.2-ReMix-SVI2-V3\Wan2.2-ReMix-SVI2-V3\ComfyUI
    ├─ models\IndexTTS2.5\        ← 10.97GB 实体（IndexTTS 2.5 真身）
    ├─ models\diffusion_models\InfiniteTalk\…  ← 数字人主模型实体
    ├─ custom_nodes\BSAI_ComfyUI_IndexTTS-2.5\  ← TTS 节点
    └─ 启动-IndexTTS25-ComfyUI.bat → http://127.0.0.1:8188/

数字人运行时（PPT 数字人栈实际启动的一套，用自带 venv）
  D:\PPT_Studio_Assets\InfiniteTalk_TTS\InfiniteTalk_Runtime\ComfyUI
    ├─ models\IndexTTS2.5          = Junction → 主包同名目录（非副本）
    └─ custom_nodes\…WanVideoWrapper / MultiTalk / …（数字人节点齐全）
  └─ 由 PPT 的 start_digital_human_stack.ps1 拉起，端口 8188

资产归档（都是链接壳）
  D:\PPT_Studio_Assets\InfiniteTalk_TTS\
    ├─ TTS\IndexTTS2.5             = Junction → 主包（非副本）
    ├─ InfiniteTalk\models\*.safetensors = HardLink → 主包模型（非副本）
    │  （仅 model.safetensors 0.35GB wav2vec 是实体）
    └─ InfiniteTalk\workflow\…      = 数字人工作流原件（本目录即其副本）
```

## 对接 DuoCast 的参数映射（阶段③）

`indextts2.5-tts_参考工作流.json` 节点入参 ↔ 本项目字段：

- `text_文本` ← 话轮 `spokenText`
- `duration_factor_语速因子`（0.5–2.0） ← 话轮 `speedRatio`
- `reference_audio_参考音频` ← A/B 绑定的参考音频（零样本克隆，阶段③「选择参考音频」控件）
- `lang_语言` = ZH
- 拼音/音素控制 ↔ 阶段②「读音词典」；8D 情感向量/`use_emo_text` ↔ 话轮 `tone`/意图
- 程序化调用：`POST /prompt`（API 格式）→ `POST /input/upload` 传参考音频 → `GET /history/{id}` + `GET /view` 取 WAV

## 约束（务必遵守）

- **不要动 PPT 项目的本地语音合成链路**：`D:\Program Files (x86)\PPT_presentation_video` 及其数字人栈（Runtime，8188）。
- 由于模型是 junction/hardlink 共享，**删除任何「看似重复」的目录都只会断引用、不省空间，并可能同时弄坏主包与 PPT**——保持现状。
- DuoCast 接引擎时，优先「读取参考工作流 + 按参数生成我们自己的 prompt 提交」，不反向写入上述目录。
