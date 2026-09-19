"""ToAPIs gpt-image-2-vip 图片适配器（链路实测 2026-09-19 验证的异步任务式流程）。

POST /v1/images/generations → 轮询 GET /v1/images/generations/{task_id} →
result.data[].url（24h 过期，立即下载落盘）→ ArtifactStore 登记 kind=image。
api 队列类（外部网络请求），不走 GPU 队列。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

BASES = ("https://toapis.com", "https://toapis.cn")


def _opener():
    # 本机 localhost 会被系统代理劫持（Windows 环境陷阱），显式直连。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ToAPIsImageProvider:
    name = "toapis-image"

    def __init__(self, api_key: str, artifacts_root: Path, artifact_store,
                 size: str = "4:5", quality: str = "low", resolution: str = "1k",
                 timeout_s: int = 600) -> None:
        self.api_key = api_key
        self.artifacts_root = Path(artifacts_root)
        self.store = artifact_store
        self.size = size
        self.quality = quality
        self.resolution = resolution
        self.timeout_s = timeout_s
        self.capabilities = {
            "simulated": False,
            "textToImage": True,
            "singleImageEdit": False,
            "multiRefEdit": False,
            "aspectRatios": [size],
            "async_": True,
            "cancel": False,
            "download": True,
            "paid": True,
        }

    async def generate(self, req: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._generate_sync, req)

    # ---- 同步实现 ----

    def _call(self, opener, url: str, data: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(url, data=body, headers=headers)
        with opener.open(request, timeout=90) as resp:
            return json.loads(resp.read())

    def _submit(self, opener, payload: dict) -> tuple[str, str]:
        last: Exception | None = None
        for base in BASES:
            try:
                task = self._call(opener, base + "/v1/images/generations", payload)
                return base, task.get("id") or task.get("data")
            except Exception as exc:  # noqa: BLE001（换下一域名重试）
                last = exc
        raise RuntimeError(f"IMAGE_SUBMIT_FAILED: 所有域名提交失败: {last}")

    def _generate_sync(self, req: dict[str, Any]) -> dict[str, Any]:
        prompt = (req.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("IMAGE_PROMPT_REQUIRED: 缺少画面描述，无法生成图片")
        if not self.api_key:
            raise RuntimeError("IMAGE_NO_KEY: 未配置 DUOCAST_TOAPIS_KEY")
        opener = _opener()
        payload = {
            "model": "gpt-image-2-vip",
            "prompt": prompt,
            "size": self.size,
            "n": 1,
            "quality": self.quality,
            "background": "auto",
            "resolution": self.resolution,
            "response_format": "url",
        }
        base, task_id = self._submit(opener, payload)
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            q = self._call(opener, f"{base}/v1/images/generations/{task_id}")
            status = q.get("status")
            if status == "completed":
                break
            if status == "failed":
                raise RuntimeError(f"IMAGE_TASK_FAILED: {json.dumps(q, ensure_ascii=False)[:300]}")
            time.sleep(5)
        else:
            raise RuntimeError(f"IMAGE_TASK_TIMEOUT: 任务 {task_id} 超过 {self.timeout_s}s 未完成")
        items = (q.get("result") or {}).get("data") or []
        url = next((i.get("url") for i in items if isinstance(i, dict) and i.get("url")), "")
        if not url:
            raise RuntimeError("IMAGE_NO_RESULT: 任务完成但没有图片 URL")
        # 结果 URL 的 CDN 会拒绝 Python-urllib 默认 UA（实测 403），用浏览器 UA 下载。
        dl_req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        image_bytes = opener.open(dl_req, timeout=120).read()
        if image_bytes[:8] not in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"):
            raise ValueError("IMAGE_BAD_BYTES: 下载内容不是 PNG/JPEG")
        artifact_id = f"IMG-{uuid.uuid4().hex[:12]}"
        path = self.artifacts_root / "image" / f"{artifact_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image_bytes)
        dependency = hashlib.sha256(json.dumps(
            {"prompt": prompt, "size": self.size, "quality": self.quality,
             "resolution": self.resolution}, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")).hexdigest()
        entry = self.store.register(artifact_id, "image", str(path), dependency,
                                    params_snapshot={"model": "gpt-image-2-vip", "taskId": task_id,
                                                     "size": self.size, "quality": self.quality})
        return {"artifactId": artifact_id, "path": str(path),
                "fileHash": entry["fileHash"], "provider": self.name,
                "taskId": task_id}
