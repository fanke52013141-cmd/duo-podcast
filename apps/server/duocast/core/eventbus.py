"""带序号事件总线（06 §5）：全局单调序号 + append-only jsonl + 有界订阅队列 + replay 补发。

事件类型沿用 01 §12.2（job.stage / job.done / job.failed / approval.requested / system.vram），
内部事件（job.queued / job.unknown）同样走本总线。前端永远不轮询进度。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

Subscriber = Callable[[dict[str, Any]], Awaitable[None]]

RESYNC_EVENT = {"seq": -1, "type": "system.resync", "payload": {}}


@dataclass
class EventBus:
    """线程安全：publish 可在任意线程调用；投递在订阅者各自的 asyncio 队列中完成。"""

    log_path: Path
    cap: int = 20_000
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _subscribers: dict[str, asyncio.Queue] = field(default_factory=dict, repr=False)
    _log_buffer: deque[dict[str, Any]] = field(default_factory=deque, repr=False)

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _persist(self, event: dict[str, Any]) -> None:
        self._log_buffer.append(event)
        if len(self._log_buffer) > self.cap:
            self._log_buffer.popleft()
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            # 简单轮转（11 报告 P2-7）：超过 16MB 归档为 .old（单代），避免 events.jsonl 无限增长
            if self.log_path.exists() and self.log_path.stat().st_size > 16 * 1024 * 1024:
                self.log_path.replace(self.log_path.with_suffix(".jsonl.old"))
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 事件日志为尽力而为；内存缓冲仍可用

    def publish(self, type_: str, **payload: Any) -> dict[str, Any]:
        event = {"seq": self._next_seq(), "type": type_, "payload": payload}
        self._persist(event)
        stale: list[str] = []
        with self._lock:
            for qid, q in self._subscribers.items():
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    stale.append(qid)
            for qid in stale:
                # 慢消费者：清空历史事件后只投递 resync 提示走快照路径（06 §5.2 / 11 报告 P2-3）
                q = self._subscribers[qid]
                while True:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    q.put_nowait(RESYNC_EVENT)
                except asyncio.QueueFull:
                    pass
        return event

    async def subscribe(self, qid: str, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers[qid] = q
        return q

    def unsubscribe(self, qid: str) -> None:
        with self._lock:
            self._subscribers.pop(qid, None)

    def resume_from_log(self) -> None:
        """启动时从 events.jsonl 尾部续接序号（12 报告 C-5）。
        序号归零会让重连客户端把新生命周期的小序号误判为连续，造成静默事件缺口。"""
        try:
            if not self.log_path.exists():
                return
            with self.log_path.open("r", encoding="utf-8", errors="replace") as fh:
                last_seq = 0
                for line in fh:
                    try:
                        seq = json.loads(line).get("seq")
                    except ValueError:
                        continue
                    if isinstance(seq, int) and seq > last_seq:
                        last_seq = seq
            with self._lock:
                self._seq = max(self._seq, last_seq)
        except OSError:
            pass

    def replay(self, after_seq: int) -> list[dict[str, Any]]:
        """按序号补发内存缓冲中的事件（06 §5.2：日志已轮转 → 调用方应返回 snapshot_required）。"""
        if after_seq > self._seq:
            return []
        return [e for e in self._log_buffer if e["seq"] > after_seq]

    @property
    def latest_seq(self) -> int:
        return self._seq
