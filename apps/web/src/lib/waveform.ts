/** Bounded, channel-independent envelope; no generated or estimated samples. */
export function audioPeaks(channels: Float32Array[], bins = 240): number[] {
  if (!channels.length || !Number.isInteger(bins) || bins < 1 || bins > 2000) throw new Error('Invalid waveform geometry');
  const length = channels[0].length;
  if (!length || channels.some(c => c.length !== length)) throw new Error('Invalid audio channels');
  const count = Math.min(bins, length);
  return Array.from({length: count}, (_, bin) => {
    const start = Math.floor(bin * length / count), end = Math.floor((bin + 1) * length / count);
    let peak = 0;
    for (const channel of channels) for (let i = start; i < end; i++) {
      const value = Math.abs(channel[i]);
      if (Number.isFinite(value)) peak = Math.max(peak, Math.min(1, value));
    }
    return peak;
  });
}

export function boundedSelection(start: number, end: number, duration: number): [number, number] | null {
  return [start, end, duration].every(Number.isFinite) && start >= 0 && end > start && end <= duration ? [start, end] : null;
}

export async function readBoundedAudio(response: Response, signal: AbortSignal, limit = 64 * 1024 * 1024) {
  if (!response.ok) throw new Error('无法读取音频文件');
  if (Number(response.headers.get('content-length')) > limit) throw new Error('音频超过64MB，请使用普通播放器试听');
  const reader = response.body?.getReader();
  if (!reader) throw new Error('浏览器不支持波形读取，请使用普通播放器');
  const chunks: Uint8Array[] = []; let size = 0;
  try {
    while (true) {
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) throw new Error('音频超过64MB，请使用普通播放器试听');
      chunks.push(value);
    }
    const data = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.length; }
    return data.buffer;
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}
