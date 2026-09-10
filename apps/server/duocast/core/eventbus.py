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
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 事件日志为尽力而为；内存缓冲仍可用

    def publish(self, type_: str, **payload: Any) -> dict[str, Any]:
        event = {"seq": self._next_seq(), "type": type_, "payload": payload}
        self._persist(event)
        stale: list[str] = []
        for qid, q in self._subscribers.items():
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                stale.append(qid)
        for qid in stale:
            # 慢消费者：丢弃历史事件，仅投递 resync 提示走快照路径（06 §5.2）
            q = self._subscribers[qid]
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(RESYNC_EVENT)
            except asyncio.QueueFull:
                pass
        return event

    async def subscribe(self, qid: str, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers[qid] = q
        return q

    def unsubscribe(self, qid: str) -> None:
        self._subscribers.pop(qid, None)

    def replay(self, after_seq: int) -> list[dict[str, Any]]:
        """按序号补发内存缓冲中的事件（06 §5.2：日志已轮转 → 调用方应返回 snapshot_required）。"""
        if after_seq > self._seq:
            return []
        return [e for e in self._log_buffer if e["seq"] > after_seq]

    @property
    def latest_seq(self) -> int:
        return self._seq
