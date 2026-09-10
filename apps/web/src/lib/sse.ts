/* ============================================================================
   DuoCast 前端 · SSE 客户端（05 §5 事件流）
   订阅 /api/events；原生 EventSource 断线自动携带 Last-Event-ID 补发；
   收到 system.resync 时由调用方回读快照（06 §5.2）。
   ========================================================================== */
import type { ServerEvent } from './types';

export interface SseHandle {
  stop: () => void;
  lastSeq: () => number;
}

export function startSse(onEvent: (ev: ServerEvent) => void, onResync?: () => void): SseHandle {
  let lastSeq = 0;
  let es: EventSource | null = null;

  es = new EventSource('/api/events');
  es.onmessage = (msg) => {
    if (msg.data === ': keepalive') return;
    let ev: ServerEvent;
    try {
      ev = JSON.parse(msg.data) as ServerEvent;
    } catch {
      return;
    }
    if (typeof ev.seq === 'number' && ev.seq > lastSeq) lastSeq = ev.seq;
    if (ev.type === 'system.resync') {
      onResync?.();
      return;
    }
    onEvent(ev);
  };
  es.onerror = () => {
    // EventSource 自动重连；服务端按 Last-Event-ID 补发，无需额外处理
  };

  return {
    stop: () => es?.close(),
    lastSeq: () => lastSeq,
  };
}
