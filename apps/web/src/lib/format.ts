/* ============================================================================
   跨页共享的格式化契约（避免各页各写一份，出现"同一时长两种写法"）。
   之前散落在 AppShell / RenderPage / VisualPage / EditScriptPage / TasksPage 五处。
   ========================================================================== */

/**
 * 秒 → 时间码。
 * - `padMinutes`：分钟补零（`01:23` vs `1:23`）
 * - `tenths`：保留一位小数（`01:23.4`）
 */
export function formatTimecode(
  seconds: number,
  opts: { padMinutes?: boolean; tenths?: boolean } = {},
): string {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  const m = Math.floor(safe / 60);
  const s = Math.floor(safe % 60);
  const mm = opts.padMinutes ? String(m).padStart(2, '0') : String(m);
  const ss = String(s).padStart(2, '0');
  if (!opts.tenths) return `${mm}:${ss}`;
  return `${mm}:${ss}.${Math.floor((safe - Math.floor(safe)) * 10)}`;
}

/** 样本数 + 采样率 → `MM:SS.d`（时间轨统一格式）。 */
export function formatSamples(samples: number, sampleRate: number): string {
  return formatTimecode(sampleRate > 0 ? samples / sampleRate : 0, { padMinutes: true, tenths: true });
}

/** 任务耗时 → `12s` / `1:30`（不足一分钟按秒取整）。 */
export function formatElapsed(seconds: number): string {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  if (safe < 60) return `${Math.round(safe)}s`;
  const m = Math.floor(safe / 60);
  return `${m}:${String(Math.round(safe % 60)).padStart(2, '0')}`;
}
