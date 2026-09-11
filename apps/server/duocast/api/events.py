"""事件订阅（01 §12.1 / 06 §5.2）：GET /api/events SSE，支持 Last-Event-ID 补发。"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

from ..core.eventbus import EventBus

router = APIRouter(prefix="/api/events", tags=["events"])


def _sse(event: dict) -> dict:
    """SSE 帧。resync 标记事件（seq=-1）不带 id，避免污染浏览器 Last-Event-ID（11 报告 P2-12）。"""
    out: dict = {"data": _serialize(event)}
    if event.get("seq", -1) >= 0:
        out["id"] = str(event["seq"])
    return out


def is_contiguous(missed: list[dict], after_seq: int) -> bool:
    """补发序列必须从 after_seq+1 开始才算无缺口；否则客户端存在静默事件缺口（11 报告 P1-3）。"""
    return bool(missed) and missed[0]["seq"] == after_seq + 1


@router.get("")
async def subscribe(request: Request) -> EventSourceResponse:
    bus: EventBus = request.app.state.event_bus
    last_seq = request.headers.get("Last-Event-ID")
    qid = uuid.uuid4().hex
    queue = await bus.subscribe(qid)

    async def gen():
        try:
            if last_seq:
                last = int(last_seq)
                missed = bus.replay(last)
                if is_contiguous(missed, last):
                    for event in missed:
                        yield _sse(event)
                elif last >= bus.latest_seq:
                    # 追平：无需补发。last > latest_seq 意味着序号来自上次服务生命周期，
                    # 落到下面 resync；last == latest_seq 才真正无缺口。
                    if last != bus.latest_seq:
                        yield {"id": "0", "data": '{"type":"system.resync"}'}
                else:
                    # 轮转缺口或跨重启：走快照路径（06 §5.2 / 11 报告 P1-3）
                    yield {"id": "0", "data": '{"type":"system.resync"}'}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"data": ": keepalive"}
                    continue
                yield _sse(event)
        finally:
            bus.unsubscribe(qid)

    return EventSourceResponse(gen())


def _serialize(event: dict) -> str:
    import json

    payload = event.get("payload", {})
    return json.dumps({"seq": event.get("seq", 0), "type": event.get("type", ""), **payload}, ensure_ascii=False)
