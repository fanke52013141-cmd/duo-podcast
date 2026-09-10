"""事件订阅（01 §12.1 / 06 §5.2）：GET /api/events SSE，支持 Last-Event-ID 补发。"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

from ..core.eventbus import EventBus

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("")
async def subscribe(request: Request) -> EventSourceResponse:
    bus: EventBus = request.app.state.event_bus
    last_seq = request.headers.get("Last-Event-ID")
    qid = uuid.uuid4().hex
    queue = await bus.subscribe(qid)

    async def gen():
        try:
            if last_seq:
                missed = bus.replay(int(last_seq))
                if missed:
                    for event in missed:
                        yield {"id": str(event["seq"]), "data": _serialize(event)}
                else:
                    # 窗口外（日志已轮转）：提示前端走快照路径（06 §5.2）
                    yield {"id": "0", "data": '{"type":"system.resync"}'}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"data": ": keepalive"}
                    continue
                yield {"id": str(event["seq"]), "data": _serialize(event)}
        finally:
            bus.unsubscribe(qid)

    return EventSourceResponse(gen())


def _serialize(event: dict) -> str:
    import json

    payload = event.get("payload", {})
    return json.dumps({"seq": event.get("seq", 0), "type": event.get("type", ""), **payload}, ensure_ascii=False)
